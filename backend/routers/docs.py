"""Document and folder routes."""
from __future__ import annotations

import logging
import shutil
import sqlite3

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile

from backend import config, db, deps, search, sync
from backend.constants import FOLDER_MAX, MAX_TXT, TITLE_MAX, UPLOAD_MAX
from backend.schemas import DocCreate, DocSave, TitleSet
from backend.services import project_files, projects
from backend.services import quota as quota_svc
from backend.services.locks import _drop_doc_locks, _named_lock
from backend.services.sidebar import doc_users, notify_sidebar
from backend.services.snapshots import auto_snap

log = logging.getLogger("typst.main")

router = APIRouter()


@router.get("/api/docs")
def list_docs(req: Request, user: str = Depends(deps.me)) -> dict:
    deps.limited(req, "sidebar")
    con = db.connect()
    try:
        own = con.execute("SELECT id, title, folder, project_id, updated_at FROM docs WHERE owner=? AND trashed=0 "
                          "ORDER BY updated_at DESC", (user,)).fetchall()
        shared = con.execute(
            "SELECT d.id,d.title,d.owner,d.folder,d.project_id,d.updated_at,COALESCE(m.role,s.role) AS role FROM docs d "
            "LEFT JOIN shares s ON s.doc_id=d.id AND s.username=? "
            "LEFT JOIN project_members m ON m.project_id=d.project_id AND m.username=? "
            "WHERE (s.username IS NOT NULL OR m.username IS NOT NULL) AND d.owner<>? AND d.trashed=0 ORDER BY d.updated_at DESC", (user,user,user)).fetchall()
        trash = con.execute("SELECT id, title, updated_at FROM docs WHERE owner=? AND trashed=1 "
                            "ORDER BY updated_at DESC", (user,)).fetchall()
        trash_items = con.execute("SELECT id,name AS title,created_at AS updated_at,kind,project_id,doc_id FROM trash_entries WHERE owner=? ORDER BY created_at DESC", (user,)).fetchall()
        return {"own": [dict(r) for r in own],
                "shared": [dict(r) for r in shared],
                "trash": [dict(r) for r in trash] + [dict(r) for r in trash_items]}
    finally:
        con.close()

@router.post("/api/docs/create")
def create_doc(b: DocCreate, req: Request, user: str = Depends(deps.me)) -> dict:
    deps.limited(req, "create")
    t = b.title.strip()
    if not t or len(t) > TITLE_MAX:
        raise HTTPException(400, f"Title: 1-{TITLE_MAX} chars")
    if len(b.content.encode("utf-8")) > MAX_TXT:
        raise HTTPException(400, "Doc too large (max 200 KB)")
    try:
        deps.check_quota(user, len(b.content.encode("utf-8")))
    except sqlite3.OperationalError as e:
        raise deps.busy_503("create_doc", e) from e
    try:
        max_docs = config.load().MAX_DOCS_PER_USER
    except Exception:
        max_docs = 100
    try:
        folder = project_files.doc_folder(b.folder[:FOLDER_MAX])
    except ValueError:
        raise HTTPException(400, "Bad folder name") from None
    did, now = db.new_id("d_"), db.now_iso()
    try:
        with db.tx() as con:
            if con.execute("SELECT COUNT(*) AS n FROM docs WHERE owner=?", (user,)).fetchone()["n"] >= max_docs:
                raise HTTPException(400, "Too many docs")
            con.execute("INSERT INTO docs (id, owner, title, content, folder, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (did, user, t, b.content,
                         folder, now, now))
            projects.attach_doc(con, did)
    except sqlite3.IntegrityError as e:
        raise HTTPException(400, "Title already exists") from e
    except sqlite3.OperationalError as e:
        raise deps.busy_503("create_doc", e) from e
    from backend.services.project_history import auto_document
    auto_document(did, actor=user)
    quota_svc.invalidate_quota_cache(user)
    notify_sidebar(user)
    return {"id": did}



@router.post("/api/docs/import-zip")
def import_zip(req: Request, f: UploadFile, name: str = Form(""), documents: str = Form("[]"), user: str = Depends(deps.me)) -> dict:
    """A new project named like the ZIP file (`name`, the frontend sends the file name)."""
    from backend.services.projectzip import import_project_zip, listed

    deps.limited(req, "create")
    return import_project_zip(user, f.file.read(UPLOAD_MAX + 1), listed(documents), name=name or f.filename or "")


@router.post("/api/zip-preview")
def zip_preview(req: Request, f: UploadFile, user: str = Depends(deps.me)) -> dict:
    from backend.services.projectzip import zip_preview as preview

    deps.limited(req, "create")
    return preview(f.file.read(UPLOAD_MAX + 1))


@router.get("/api/docs/{doc_id}")
def get_doc(doc_id: str, req: Request, user: str = Depends(deps.me)) -> dict:
    deps.limited(req, "files_list")
    deps.check_doc_id(doc_id)
    r = deps.need_access(user, doc_id)
    con = db.connect()
    try:
        d = con.execute("SELECT id, owner, title, content, folder, project_id, trashed, updated_at FROM docs WHERE id=?",
                        (doc_id,)).fetchone()
        if not d:
            raise HTTPException(404, "Doc gone")
        users = con.execute("SELECT username, role FROM shares WHERE doc_id=?", (doc_id,)).fetchall()
        return {**dict(d), "role": r, "shares": [dict(u) for u in users]}
    finally:
        con.close()


@router.post("/api/docs/{doc_id}/save")
async def save_doc(doc_id: str, b: DocSave, req: Request, user: str = Depends(deps.me)) -> dict:
    deps.check_doc_id(doc_id)
    deps.limited(req, "save")
    deps.need_edit(user, doc_id)
    live = sync.room_text(doc_id)
    if live is not None and live != b.content and not b.force:
        raise HTTPException(409, "Newer live-room state - save with force")
    content = b.content if (live is None or b.force) else live
    if len(content.encode("utf-8")) > MAX_TXT:
        raise HTTPException(400, "Doc too large (max 200 KB)")
    try:
        _c = db.connect()
        try:
            _old = _c.execute("SELECT content FROM docs WHERE id=?", (doc_id,)).fetchone()
        finally:
            _c.close()
    except sqlite3.OperationalError as e:
        raise deps.busy_503("save_doc", e) from e
    _old_text = (_old["content"] or "") if _old else ""
    _old_len = len(_old_text.encode("utf-8"))
    _save_owner = deps._owner(doc_id, user)

    def _restore_old() -> None:
        _rb = db.connect()
        try:
            _rb.execute("UPDATE docs SET content=?, updated_at=? WHERE id=?",
                        (_old_text, db.now_iso(), doc_id))
            _rb.commit()
        finally:
            _rb.close()

    try:
        with quota_svc.quota_guard(_save_owner, max(0, len(content.encode("utf-8")) - _old_len),
                                   rollback=_restore_old):
            try:
                with db.tx() as con:
                    trashed = con.execute("SELECT trashed FROM docs WHERE id=?", (doc_id,)).fetchone()
                    if not trashed:
                        raise HTTPException(404, "Doc gone")
                    if trashed["trashed"]:
                        raise HTTPException(410, "In trash - restore first")
                    # Pre-tx need_edit is a fast-path only; the role must
                    # hold on THIS connection (unshare/downgrade may land in
                    # the gap between check and BEGIN IMMEDIATE).
                    if deps._tx_role(con, user, doc_id) not in ("owner", "editor"):
                        raise HTTPException(403, "Reviewer can only comment")
                    con.execute("UPDATE docs SET content=?, updated_at=? WHERE id=?",
                                (content, db.now_iso(), doc_id))
            except sqlite3.OperationalError as e:
                raise deps.busy_503("save_doc", e) from e
    except sqlite3.OperationalError as e:
        raise deps.busy_503("save_doc", e) from e
    except HTTPException as e:
        if e.status_code == 413 and live is not None:
            await sync.replace_text(doc_id, _old_text)
        raise
    if b.force:
        await sync.replace_text(doc_id, content)
    elif live is None:
        if not sync.persist(doc_id):
            db.clear_room_state(doc_id)
    else:
        sync.persist(doc_id)
    try:
        auto_snap(doc_id, content, actor=user)
    except HTTPException as e:
        log.warning("save_doc snap: %s", e.detail)
    return {"ok": True}


@router.post("/api/docs/{doc_id}/rename")
def rename_doc(doc_id: str, b: TitleSet, req: Request, user: str = Depends(deps.me)) -> dict:
    deps.check_doc_id(doc_id)
    deps.limited(req, "rename")
    t = b.title.strip()
    if not t or len(t) > TITLE_MAX:
        raise HTTPException(400, f"Title: 1-{TITLE_MAX} chars")
    try:
        with db.tx() as con:
            if deps._tx_role(con, user, doc_id) not in ("owner", "editor"):
                raise HTTPException(403, "Only project editors can rename")
            row = con.execute("SELECT project_id,trashed FROM docs WHERE id=?", (doc_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Doc gone")
            if row["trashed"]:
                raise HTTPException(410, "In trash - restore first")
            if row["project_id"]:
                projects.require(con, row["project_id"], user, edit=True)
            con.execute("UPDATE docs SET title=?, updated_at=? WHERE id=?", (t, db.now_iso(), doc_id))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Title already exists") from None
    for recipient in doc_users(doc_id):
        notify_sidebar(recipient)
    return {"ok": True}


@router.delete("/api/docs/{doc_id}")
async def delete_doc(doc_id: str, req: Request, user: str = Depends(deps.me)) -> dict:
    return await _delete_doc(doc_id, req, user)


@router.post("/api/docs/{doc_id}/trash")
async def trash_doc(doc_id: str, req: Request, user: str = Depends(deps.me)) -> dict:
    return await _delete_doc(doc_id, req, user, trash_only=True)


async def _delete_doc(doc_id: str, req: Request, user: str, trash_only: bool = False) -> dict:
    deps.check_doc_id(doc_id)
    deps.limited(req, "delete")
    deps.need_access(user, doc_id, allow_trashed=True)
    audience = doc_users(doc_id)
    gone = ""
    with db.tx() as con:
        row = con.execute("SELECT trashed,project_id,owner FROM docs WHERE id=?", (doc_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Doc gone")
        role = db.role_on(con, user, doc_id)
        if not role:
            raise HTTPException(404, "Doc gone")
        if role not in ("owner", "editor"):
            raise HTTPException(403, "Only editors can delete")
        trashed = bool(row["trashed"])
        if role != "owner":
            if (trashed and not trash_only) or not row["project_id"]:
                raise HTTPException(403, "Only owner can permanently delete")
            projects.require(con, row["project_id"], user, edit=True)
        if trashed and trash_only:
            return {"trashed": True}
        if not trashed:
            con.execute("UPDATE docs SET trashed=1,updated_at=? WHERE id=?", (db.now_iso(), doc_id))
        else:
            # Permanent delete: comments go with the doc (FK cascade); the
            # project and its files go once no document is left.
            con.execute("DELETE FROM docs WHERE id=?", (doc_id,))
            con.execute("DELETE FROM trash_entries WHERE doc_id=?", (doc_id,))
            if row["project_id"] and con.execute("DELETE FROM projects WHERE id=? AND archived=1 AND NOT EXISTS (SELECT 1 FROM docs WHERE project_id=?)",
                                                 (row["project_id"], row["project_id"])).rowcount:
                gone = row["project_id"]
    await sync.drop(doc_id)
    quota_svc.invalidate_quota_cache(row["owner"])
    _drop_doc_locks(doc_id)
    for recipient in audience:
        notify_sidebar(recipient)
    if gone:
        shutil.rmtree(deps.get_files_dir() / gone, ignore_errors=True)
    return {"trashed": not trashed}


@router.post("/api/docs/{doc_id}/restore")
def restore_doc(doc_id: str, req: Request, user: str = Depends(deps.me)) -> dict:
    deps.check_doc_id(doc_id)
    deps.limited(req, "restore")
    deps.need_access(user, doc_id, allow_trashed=True)
    try:
        with db.tx() as con:
            cur = con.execute("SELECT folder,project_id,owner FROM docs WHERE id=?", (doc_id,)).fetchone()
            if cur is None:
                raise HTTPException(404, "Doc gone")
            if cur["owner"] != user:
                raise HTTPException(403, "Only owner can restore")
            fld = cur["folder"] or ""
            if fld and not cur["project_id"] and not con.execute("SELECT 1 FROM folders WHERE owner=? AND kind='doc' AND name=?", (user, fld)).fetchone():
                fld = ""
            if cur["project_id"]:
                con.execute("UPDATE projects SET archived=0 WHERE id=?", (cur["project_id"],))
            con.execute("UPDATE docs SET trashed=0,folder=?,updated_at=? WHERE id=?", (fld, db.now_iso(), doc_id))
    except sqlite3.IntegrityError as e:
        raise HTTPException(409, "Name already exists in this folder; rename the existing document before restoring") from e
    for recipient in doc_users(doc_id):
        notify_sidebar(recipient)
    return {"ok": True}

@router.post("/api/docs/{doc_id}/duplicate")
def duplicate_doc(doc_id: str, req: Request, user: str = Depends(deps.me)) -> dict:
    deps.check_doc_id(doc_id)
    deps.limited(req, "duplicate")
    lang = deps.req_lang(req)
    deps.need_edit(user, doc_id)
    try:
        max_docs = config.load().MAX_DOCS_PER_USER
    except Exception:
        max_docs = 100
    nid, now = db.new_id("d_"), db.now_iso()
    backups: project_files.Backups = {}
    with _named_lock(f"dup:{user}"):
        try:
            with db.tx() as con:
                # Outer need_edit is a fast-path only; re-check on the tx
                # connection before inserting (role may change in the gap).
                if deps._tx_role(con, user, doc_id) not in ("owner", "editor"):
                    raise HTTPException(403, "Reviewer can only comment")
                d = con.execute("SELECT title, content, folder, trashed, owner, project_id FROM docs WHERE id=?",
                                (doc_id,)).fetchone()
                if not d:
                    raise HTTPException(404, "Doc gone")
                if d["trashed"]:
                    raise HTTPException(410, "In trash - restore first")
                target_owner = d["owner"]
                guest_copy = not d["project_id"]
                if d["project_id"]:
                    try:
                        projects.require(con, d["project_id"], user, edit=True)
                    except HTTPException as e:
                        if e.status_code != 404:
                            raise
                        guest_copy = True
                if guest_copy:
                    target_owner = user
                if con.execute("SELECT COUNT(*) FROM docs WHERE owner=?", (target_owner,)).fetchone()[0] >= max_docs:
                    raise HTTPException(400, "Too many docs")
                base = d["title"] + (" (Kopie)" if lang == "de" else " (copy)")
                title, i = base, 2
                pid = d["project_id"]
                while not guest_copy and con.execute("SELECT 1 FROM docs WHERE project_id=? AND folder=? AND title=? COLLATE NOCASE AND trashed=0",
                                                     (pid, d["folder"] or "", title)).fetchone():
                    title, i = f"{base} {i}", i + 1
                    if i > 99:
                        raise HTTPException(400, "Too many copies")
                text = project_files.doc_text(doc_id, d["content"])
                if guest_copy and pid:
                    # A guest's copy lands in a project of their own with the files the document reaches.
                    reached = project_files.visible(con, pid, user) or set()
                    new_pid = projects.create(con, target_owner, title)
                    for path in sorted(reached):
                        project_files.put(con, new_pid, path, project_files.read(pid, path), backups)
                    pid = new_pid
                con.execute("INSERT INTO docs (id, owner, title, content, folder, project_id, created_at, updated_at) "
                            "VALUES (?,?,?,?,?,?,?,?)",
                            (nid, target_owner, title, text, d["folder"], pid, now, now))
                if not pid:  # a document from before projects: its copy gets one, with the templates it uses
                    pid = projects.attach_doc(con, nid)
                quota_svc.check_quota(target_owner, 0, con)
        except Exception as e:
            project_files.undo(backups)
            if isinstance(e, sqlite3.IntegrityError):
                raise HTTPException(400, "Title already exists") from e
            if isinstance(e, sqlite3.OperationalError):
                raise deps.busy_503("duplicate_doc", e) from e
            raise
    quota_svc.invalidate_quota_cache(target_owner)
    if pid:
        projects.notify(pid)
    notify_sidebar(user)
    return {"id": nid}


@router.get("/api/search")
def search_docs(req: Request, q: str = "", user: str = Depends(deps.me)) -> dict:
    deps.limited(req, "search")
    con = db.connect()
    try:
        rows = search.search_visible(user, q, con, deps.get_files_dir(), sync.room_text)
    finally:
        con.close()
    return {"hits": [{k: h[k] for k in ("id", "title", "owner", "snippet", "pos")} for h in rows]}


@router.get("/api/search/content")
def search_content(req: Request, q: str = "", user: str = Depends(deps.me)) -> dict:
    """Hit counts per entry for the sidebar's optional content search."""
    deps.limited(req, "search")
    con = db.connect()
    try:
        entries = search.content_visible(user, q, con, deps.get_files_dir(), sync.room_text)
    finally:
        con.close()
    return {"entries": entries}


@router.post("/api/trash/{entry_id}/restore")
def restore_trash_entry(entry_id: str, req: Request, user: str = Depends(deps.me)) -> dict:
    from backend.services import trash

    deps.limited(req, "restore")
    return trash.restore(user, entry_id)


@router.delete("/api/trash")
async def empty_trash(req: Request, user: str = Depends(deps.me)) -> dict:
    deps.limited(req, "delete")
    with db.tx() as con:
        documents = con.execute("SELECT id,project_id FROM docs WHERE owner=? AND trashed=1", (user,)).fetchall()
        audience = {user}
        for document in documents:
            audience.update(doc_users(document["id"]))
        con.execute("DELETE FROM trash_entries WHERE owner=? OR doc_id IN (SELECT id FROM docs WHERE owner=? AND trashed=1)",
                    (user, user))
        con.execute("DELETE FROM docs WHERE owner=? AND trashed=1", (user,))
        gone = [project for project in {d["project_id"] for d in documents if d["project_id"]}
                if con.execute("DELETE FROM projects WHERE id=? AND archived=1 AND NOT EXISTS (SELECT 1 FROM docs WHERE project_id=?)",
                               (project, project)).rowcount]
    quota_svc.invalidate_quota_cache(user)
    for document in documents:
        doc_id = document["id"]
        await sync.drop(doc_id)
        _drop_doc_locks(doc_id)
    for project in gone:
        shutil.rmtree(deps.get_files_dir() / project, ignore_errors=True)
    for recipient in audience:
        notify_sidebar(recipient)
    return {"documents": [d["id"] for d in documents]}


@router.delete("/api/trash/{entry_id}")
def delete_trash_entry(entry_id: str, req: Request, user: str = Depends(deps.me)) -> dict:
    from backend.services.locks import _named_lock

    deps.limited(req, "delete")
    with _named_lock("trash:" + entry_id):
        with db.tx() as con:
            row = con.execute("SELECT project_id,doc_id FROM trash_entries WHERE id=? AND owner=?",
                              (entry_id, user)).fetchone()
            if not row:
                raise HTTPException(404, "Trash entry gone")
            con.execute("DELETE FROM trash_entries WHERE id=?", (entry_id,))
    quota_svc.invalidate_quota_cache(user)
    notify_sidebar(user)
    if row["project_id"]:
        projects.notify(row["project_id"])
    if row["doc_id"]:
        from backend.services.sidebar import emit_doc
        emit_doc(row["doc_id"], "file")
    return {"ok": True}
