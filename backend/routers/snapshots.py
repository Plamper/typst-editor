"""Version routes: document and project versions, version settings and the Git mirror."""
from __future__ import annotations

import asyncio
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request

from backend import config, db, deps, sync
from backend.schemas import SnapNew, SnapRename, SnapRestore, VersionSettings
from backend.services import quota
from backend.services.snapshots import prune_snaps

router = APIRouter()


@router.get("/api/docs/{doc_id}/snapshots")
def list_snaps(doc_id: str, req: Request, user: str = Depends(deps.me)) -> dict:
    deps.limited(req, "files_list")
    deps.check_doc_id(doc_id)
    deps.need_access(user, doc_id)
    con = db.connect()
    try:
        rows = con.execute("SELECT id, label, created_at, LENGTH(content) AS size FROM snapshots "
                           "WHERE doc_id=? ORDER BY created_at DESC, rowid DESC", (doc_id,)).fetchall()
        return {"snapshots": [dict(r) for r in rows]}
    finally:
        con.close()


@router.post("/api/docs/{doc_id}/snapshots")
def make_snap(doc_id: str, b: SnapNew, req: Request, user: str = Depends(deps.me)) -> dict:
    deps.check_doc_id(doc_id)
    deps.limited(req, "snapshots")
    deps.need_edit(user, doc_id)
    live = sync.room_text(doc_id)
    with db.tx() as con:
        if deps._tx_role(con, user, doc_id) not in ("owner", "editor"):
            raise HTTPException(403, "Reviewer can only comment")
        row = con.execute("SELECT content FROM docs WHERE id=?", (doc_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Doc gone")
        cur = live if live is not None else row["content"]
        owner = con.execute("SELECT owner FROM docs WHERE id=?", (doc_id,)).fetchone()[0]
        sid = db.new_id("s_")
        con.execute("INSERT INTO snapshots (id, doc_id, content, label, created_at) VALUES (?,?,?,?,?)",
                    (sid, doc_id, cur, b.label.strip()[:80], db.now_iso()))
        prune_snaps(con, doc_id)
        quota.check_quota(owner, 0, con)
    quota.invalidate_quota_cache(owner)
    return {"id": sid}


@router.get("/api/docs/{doc_id}/snapshots/{sid}")
def get_snap(doc_id: str, sid: str, req: Request, user: str = Depends(deps.me)) -> dict:
    deps.limited(req, "files_list")
    deps.check_doc_id(doc_id)
    deps.need_access(user, doc_id)
    con = db.connect()
    try:
        r = con.execute("SELECT content, label, created_at FROM snapshots WHERE id=? AND doc_id=?",
                        (sid, doc_id)).fetchone()
        if not r:
            raise HTTPException(404, "Version not found")
        return dict(r)
    finally:
        con.close()


@router.post("/api/docs/{doc_id}/snapshots/{sid}/restore")
async def restore_snap(doc_id: str, sid: str, req: Request, user: str = Depends(deps.me),
                       force: bool = False, b: SnapRestore | None = None) -> dict:
    deps.check_doc_id(doc_id)
    deps.limited(req, "snapshots")
    deps.need_edit(user, doc_id)
    eff = force or (b.force if b else False)  # query or body flag, default safe
    auto = sync.room_text(doc_id)
    try:
        with db.tx() as con:
            # Outer need_edit is a fast-path only; re-check on the tx
            # connection before restoring (role may change in the gap).
            if deps._tx_role(con, user, doc_id) not in ("owner", "editor"):
                raise HTTPException(403, "Reviewer can only comment")
            s = con.execute("SELECT content FROM snapshots WHERE id=? AND doc_id=?", (sid, doc_id)).fetchone()
            if not s:
                raise HTTPException(404, "Version not found")
            cur = con.execute("SELECT content, trashed FROM docs WHERE id=?", (doc_id,)).fetchone()
            if not cur:
                raise HTTPException(404, "Doc gone")
            if cur["trashed"]:
                raise HTTPException(410, "In trash - restore first")
            if auto is not None and auto != (cur["content"] or "") and not eff:
                raise HTTPException(409, "Document changed meanwhile, retry with force")
            before = auto if auto is not None else cur["content"]
            owner = con.execute("SELECT owner FROM docs WHERE id=?", (doc_id,)).fetchone()[0]
            con.execute("INSERT INTO snapshots (id, doc_id, content, label, created_at) VALUES (?,?,?,?,?)",
                        (db.new_id("s_"), doc_id, before, "Before restore", db.now_iso()))
            prune_snaps(con, doc_id)
            quota.check_quota(owner, max(0, len((s["content"] or "").encode("utf-8")) - len((cur["content"] or "").encode("utf-8"))), con)
            con.execute("UPDATE docs SET content=?, updated_at=? WHERE id=?", (s["content"], db.now_iso(), doc_id))
    except sqlite3.OperationalError as e:
        raise deps.busy_503("restore_snap", e) from e
    await sync.replace_text(doc_id, s["content"])
    quota.invalidate_quota_cache(owner)
    return {"ok": True}


@router.delete("/api/docs/{doc_id}/snapshots/{sid}")
def delete_snap(doc_id: str, sid: str, req: Request, user: str = Depends(deps.me)) -> dict:
    deps.check_doc_id(doc_id)
    deps.limited(req, "snapshots")
    with db.tx() as con:
        if deps._tx_role(con, user, doc_id) != "owner":
            raise HTTPException(403, "Only the owner can delete versions")
        con.execute("DELETE FROM snapshots WHERE id=? AND doc_id=?", (sid, doc_id))
    quota.invalidate_quota_cache(user)
    return {"ok": True}


@router.get("/api/projects/{project_id}/snapshots")
def list_project_snaps(project_id: str, req: Request, user: str = Depends(deps.me)) -> dict:
    from backend.services import projects
    deps.limited(req, "files_list")
    con = db.connect()
    try:
        from backend.services import project_git, project_history
        projects.require(con, project_id, user)
        rows = [dict(r) for r in con.execute("SELECT id,label,kind,author,summary,editors,commit_hash,created_at,LENGTH(CAST(content AS BLOB)) AS size FROM project_snapshots WHERE project_id=? ORDER BY created_at DESC,rowid DESC", (project_id,))]
        s = project_history.settings(con, project_id)
        pushed = project_git.pushed(project_id, s["pushed_hash"]) if s["remote"] else set()
        for r in rows:
            r["pushed"] = bool(r["commit_hash"]) and r["commit_hash"] in pushed
        return {"snapshots": rows, "git": bool(s["git"]), "remote": bool(s["git"] and s["remote"])}
    finally:
        con.close()


@router.post("/api/projects/{project_id}/snapshots")
def make_project_snap(project_id: str, b: SnapNew, req: Request, user: str = Depends(deps.me)) -> dict:
    from backend.services import notifications, project_git, project_history, projects, quota
    deps.limited(req, "snapshots")
    with project_history.prepared(project_id, user) as (state, expected), db.tx() as con:
        projects.require(con, project_id, user, edit=True, note=False)  # saving a version changes nothing: no co-author for it
        project_history.unchanged(con, project_id, expected)
        sid = project_history.save(con, project_id, state, b.label.strip(), author=user, editors=notifications.take_writers(project_id))
        owner = con.execute("SELECT owner FROM projects WHERE id=?", (project_id,)).fetchone()[0]
    quota.invalidate_quota_cache(owner)
    project_git.schedule(project_id)
    return {"id": sid}


@router.get("/api/projects/{project_id}/snapshots/{sid}")
def get_project_snap(project_id: str, sid: str, req: Request, against: str = "current", user: str = Depends(deps.me)) -> dict:
    """Changes from checkpoint `sid` to `against` ("current" = the live project, or another checkpoint id)."""

    from backend.services import project_history, projects
    deps.limited(req, "files_list")
    con = db.connect()
    try:
        projects.require(con, project_id, user)
        row = con.execute("SELECT content,label,kind,author,created_at FROM project_snapshots WHERE id=? AND project_id=?", (sid, project_id)).fetchone()
        if not row:
            raise HTTPException(404, "Version not found")
        other = None
        if against != "current":
            other = con.execute("SELECT content,label,created_at FROM project_snapshots WHERE id=? AND project_id=?", (against, project_id)).fetchone()
            if not other:
                raise HTTPException(404, "Version not found")
        saved = project_history.load(row["content"])
        if other:
            current = project_history.load(other["content"])
        else:
            with project_history.prepared(project_id, user) as (current, _):
                pass
        changes = project_history.changes(saved, current)
        return {**dict(row), "content": project_history.diff_text(saved), "current_content": project_history.diff_text(current), "changes": changes,
                "against": {"id": against, "label": other["label"] if other else "", "created_at": other["created_at"] if other else ""}}
    finally:
        con.close()


@router.patch("/api/projects/{project_id}/snapshots/{sid}")
def rename_project_snap(project_id: str, sid: str, b: SnapRename, req: Request, user: str = Depends(deps.me)) -> dict:
    """Name a checkpoint. A named automatic checkpoint becomes permanent (kind 'manual')."""
    from backend.services import projects
    deps.limited(req, "snapshots")
    label = b.label.strip()
    with db.tx() as con:
        role = projects.require(con, project_id, user, edit=True, note=False)
        row = con.execute("SELECT kind,author FROM project_snapshots WHERE id=? AND project_id=?", (sid, project_id)).fetchone()
        if not row:
            raise HTTPException(404, "Version not found")
        if role != "owner" and user != row["author"]:
            raise HTTPException(403, "Only the owner or the author can rename a version")
        kind = "manual" if label else row["kind"]
        con.execute("UPDATE project_snapshots SET label=?,kind=? WHERE id=?", (label, kind, sid))
    return {"label": label, "kind": kind}


@router.delete("/api/projects/{project_id}/snapshots/{sid}")
def delete_project_snap(project_id: str, sid: str, req: Request, user: str = Depends(deps.me)) -> dict:
    from backend.services import projects, quota
    deps.limited(req, "snapshots")
    with db.tx() as con:
        projects.require(con, project_id, user, edit=True, note=False)
        owner = con.execute("SELECT owner FROM projects WHERE id=?", (project_id,)).fetchone()[0]
        row = con.execute("SELECT author FROM project_snapshots WHERE project_id=? AND id=?", (project_id, sid)).fetchone()
        if not row:
            raise HTTPException(404, "Version not found")
        if user != owner and user != row["author"]:
            raise HTTPException(403, "Only the owner or the author can delete a version")
        con.execute("DELETE FROM project_snapshots WHERE project_id=? AND id=?", (project_id, sid))
    quota.invalidate_quota_cache(owner)
    return {"ok": True}


def _restore_project_state(project_id: str, sid: str, b: SnapRestore, user: str) -> tuple[dict, list[str], str]:
    import base64

    from backend.services import project_files, project_history, projects, quota

    backups: project_files.Backups = {}
    with project_files.lock(project_id):
        con = db.connect()
        try:
            con.execute('BEGIN')
            projects.require(con, project_id, user, edit=True)
            expected_stamp = project_history.stamp(con, project_id)
            before = project_history.capture(con, project_id)
        finally:
            con.close()
        try:
            with db.tx() as con:
                projects.require(con, project_id, user, edit=True)
                project_history.unchanged(con, project_id, expected_stamp)
                row = con.execute("SELECT content FROM project_snapshots WHERE id=? AND project_id=?", (sid, project_id)).fetchone()
                if not row:
                    raise HTTPException(404, "Version not found")
                state = project_history.load(row['content'])
                owner = con.execute("SELECT owner FROM projects WHERE id=?", (project_id,)).fetchone()[0]
                current = [r[0] for r in con.execute("SELECT id FROM docs WHERE project_id=?", (project_id,))]
                for d in state['documents']:
                    existing = con.execute('SELECT project_id FROM docs WHERE id=?', (d['id'],)).fetchone()
                    if existing and existing[0] != project_id:
                        raise HTTPException(409, 'Document belongs to another project')
                # A live collaborator must not be silently overwritten.
                for did in current:
                    live = sync.room_text(did)
                    saved = con.execute('SELECT content FROM docs WHERE id=?', (did,)).fetchone()[0]
                    if live is not None and live != saved and not b.force:
                        raise HTTPException(409, 'Project has unsaved live changes; retry with force')
                # The safety net must survive auto-retention, and the final
                # quota check below accounts for it; a plain cap check here
                # would reject restores that free space.
                project_history.save(con, project_id, before, 'Before restore', kind='manual', author=user, check=False)
                new_docs = sum(not con.execute('SELECT 1 FROM docs WHERE id=?', (d['id'],)).fetchone() for d in state['documents'])
                if new_docs + con.execute('SELECT COUNT(*) FROM docs WHERE owner=?', (owner,)).fetchone()[0] > config.load().MAX_DOCS_PER_USER:
                    raise HTTPException(400, 'Too many docs')
                con.execute('UPDATE docs SET trashed=1 WHERE project_id=?', (project_id,))
                for d in state['documents']:
                    try:  # a stored checkpoint is data: its folder passes the same rule as a new one
                        d['folder'] = project_files.doc_folder(d['folder'])
                    except ValueError:
                        d['folder'] = ''
                    if not con.execute('SELECT 1 FROM docs WHERE id=?', (d['id'],)).fetchone():
                        con.execute('INSERT INTO docs(id,owner,title,folder,content,project_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)',
                                    (d['id'], owner, d['title'], d['folder'], d['content'], project_id, d.get('created_at') or db.now_iso(), db.now_iso()))
                    else:
                        con.execute('UPDATE docs SET title=?,folder=?,content=?,trashed=0,yjs=NULL,updated_at=? WHERE id=?', (d['title'], d['folder'], d['content'], db.now_iso(), d['id']))
                for path in [r[0] for r in con.execute('SELECT path FROM project_files WHERE project_id=?', (project_id,))]:
                    if path not in state['files']:
                        project_files.remove(con, project_id, path, backups)
                for path, encoded in state['files'].items():
                    project_files.put(con, project_id, path, base64.b64decode(encoded), backups)
                quota.check_quota(owner, 0, con)
        except Exception as error:
            project_files.undo(backups)
            if isinstance(error, sqlite3.IntegrityError):
                raise HTTPException(409, "A document name conflicts in this project; rename it before restoring") from error
            raise
    return state, current, owner


@router.post("/api/projects/{project_id}/snapshots/{sid}/restore")
async def restore_project_snap(project_id: str, sid: str, b: SnapRestore, req: Request, user: str = Depends(deps.me)) -> dict:
    from backend.services import project_git, projects, quota

    deps.limited(req, "snapshots")
    state, current, owner = await asyncio.to_thread(_restore_project_state, project_id, sid, b, user)
    project_git.schedule(project_id)
    for d in state['documents']:
        await sync.replace_text(d['id'], d['content'])
    for did in set(current) - {d['id'] for d in state['documents']}:
        await sync.drop(did)
    quota.invalidate_quota_cache(owner)
    await asyncio.to_thread(projects.notify, project_id)
    return {"ok": True, "documents": [d['id'] for d in state['documents']]}


def _versioning(con: sqlite3.Connection, project_id: str) -> dict:
    from backend.services import project_git, project_history
    s = project_history.settings(con, project_id)
    return {"auto": bool(s["auto"]), "auto_minutes": s["auto_minutes"], "auto_keep": s["auto_keep"], "git": bool(s["git"]), "git_available": project_git.available(),
            "remote": s["remote"], "branch": s["branch"], "has_token": bool(s["token"]), "push": bool(s["push"]),
            "push_status": s["push_status"], "pushed_at": s["pushed_at"]}


@router.get("/api/projects/{project_id}/versioning")
def get_versioning(project_id: str, req: Request, user: str = Depends(deps.me)) -> dict:
    from backend.services import projects
    deps.limited(req, "files_list")
    con = db.connect()
    try:
        projects.require(con, project_id, user, owner=True)
        return _versioning(con, project_id)
    finally:
        con.close()


@router.put("/api/projects/{project_id}/versioning")
def set_versioning(project_id: str, b: VersionSettings, req: Request, user: str = Depends(deps.me)) -> dict:
    from backend.services import project_git, project_history, projects
    deps.limited(req, "snapshots")
    if b.git and not project_git.available():
        raise HTTPException(400, "Git is not installed on this server")
    branch = project_git.check_branch(b.branch)
    con = db.connect()
    try:
        projects.require(con, project_id, user, owner=True)
        old = project_history.settings(con, project_id)
    finally:
        con.close()
    remote = b.remote.strip() if b.remote.strip() == old["remote"] else project_git.check_remote(b.remote)  # resolving runs outside the tx
    with db.tx() as con:
        projects.require(con, project_id, user, owner=True)
        token = old["token"] if b.token is None else project_git.seal(b.token.strip())
        con.execute("INSERT INTO project_versioning (project_id,auto,auto_minutes,auto_keep,git,remote,branch,token,push) VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(project_id) DO UPDATE SET auto=excluded.auto,auto_minutes=excluded.auto_minutes,auto_keep=excluded.auto_keep,git=excluded.git,"
                    "remote=excluded.remote,branch=excluded.branch,token=excluded.token,push=excluded.push",
                    (project_id, int(b.auto), b.auto_minutes, b.auto_keep, int(b.git), remote, branch, token, int(b.push)))
        if remote != old["remote"] or branch != old["branch"]:
            con.execute("UPDATE project_versioning SET push_status='',pushed_at='',pushed_hash='' WHERE project_id=?", (project_id,))
        project_history.retain(con, project_id)
        owner = con.execute("SELECT owner FROM projects WHERE id=?", (project_id,)).fetchone()[0]
        out = _versioning(con, project_id)
    quota.invalidate_quota_cache(owner)
    if b.git:
        project_git.schedule(project_id)
    return out


@router.post("/api/projects/{project_id}/snapshots/{sid}/{action}")
async def git_version(project_id: str, sid: str, action: str, req: Request, user: str = Depends(deps.me)) -> dict:
    """Commit a version that has no commit yet, or push one that the remote does not have."""
    from backend.services import project_git, projects
    if action not in ("commit", "push"):
        raise HTTPException(404, "Not found")
    deps.limited(req, "snapshots")
    con = db.connect()
    try:
        projects.require(con, project_id, user, owner=True)
        s = _versioning(con, project_id)
        if not con.execute("SELECT 1 FROM project_snapshots WHERE id=? AND project_id=?", (sid, project_id)).fetchone():
            raise HTTPException(404, "Version not found")
    finally:
        con.close()
    if not s["git"] or (action == "push" and not s["remote"]):
        raise HTTPException(400, "Turn on Git and set a remote first" if action == "push" else "Turn on Git first")
    try:
        status = await asyncio.to_thread(project_git.run_now, project_git.commit_version if action == "commit" else project_git.push_version, project_id, sid)
    except TimeoutError as e:
        raise HTTPException(504, "Git timed out") from e
    if status:
        raise HTTPException(409, status)
    return {"ok": True}
