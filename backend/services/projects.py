"""Project membership, creation and the sidebar payload."""
from __future__ import annotations

import posixpath
import sqlite3

from fastapi import HTTPException

from backend import db


def role(con: sqlite3.Connection, project_id: str, user: str) -> str:
    p = con.execute("SELECT owner,archived FROM projects WHERE id=?", (project_id,)).fetchone()
    if not p or p["archived"]:
        raise HTTPException(404, "Project not found")
    if p["owner"] == user:
        return "owner"
    m = con.execute("SELECT role FROM project_members WHERE project_id=? AND username=?",
                    (project_id, user)).fetchone()
    if not m:
        raise HTTPException(404, "Project not found")
    return m["role"]


def require(con: sqlite3.Connection, project_id: str, user: str, edit: bool = False,
            owner: bool = False, note: bool = True) -> str:
    r = role(con, project_id, user)
    if edit and con.execute("SELECT archived FROM projects WHERE id=?", (project_id,)).fetchone()[0]:
        raise HTTPException(410, "Project is in trash; restore a document first")
    if (owner and r != "owner") or (edit and r not in ("owner", "editor")):
        raise HTTPException(403, "Insufficient project permissions")
    if edit and note:
        from backend.services.notifications import note_editor
        note_editor(project_id, user)
    return r


def notify(project_id: str) -> None:
    from backend.services.sidebar import emit_doc, notify_sidebar

    con = db.connect()
    try:
        p = con.execute("SELECT owner,archived FROM projects WHERE id=?", (project_id,)).fetchone()
        users = [r[0] for r in con.execute("SELECT username FROM project_members WHERE project_id=?", (project_id,))]
        docs = [r[0] for r in con.execute("SELECT id FROM docs WHERE project_id=?", (project_id,))]
    finally:
        con.close()
    if p:
        from backend.services.project_history import auto
        auto(project_id)
        for user in [p["owner"], *users]:
            notify_sidebar(user)
        for doc_id in docs:
            emit_doc(doc_id, "file")


def create(con, owner: str, title: str, archived: bool = False) -> str:
    base, suffix = title, 2
    while con.execute("SELECT 1 FROM projects WHERE owner=? AND title=? COLLATE NOCASE", (owner, title)).fetchone():
        tail = f" ({suffix})"
        title = base[:100-len(tail)] + tail
        suffix += 1
    pid = db.new_id("p_")
    con.execute("INSERT INTO projects(id,owner,title,created_at,archived) VALUES (?,?,?,?,?)",
                (pid, owner, title, db.now_iso(), int(archived)))
    return pid


def attach_doc(con, doc_id: str, project_id: str | None = None, *, check_quota: bool = True) -> str:
    """Put a document into a project (a new one named after it unless given). Private templates it
    quotes are snapshotted as project files where the reference resolves; existing files win."""
    from backend.services import project_files, quota, resources
    d = con.execute("SELECT * FROM docs WHERE id=?", (doc_id,)).fetchone()
    if d["project_id"] and not project_id:
        return d["project_id"]
    if d["project_id"] and d["project_id"] != project_id:
        raise HTTPException(409, "Document belongs to another project")
    pid = project_id
    if not pid:
        title = d["folder"].split("/")[0] if d["folder"] else d["title"]
        if d["folder"]:
            row = con.execute("SELECT project_id FROM legacy_project_folders WHERE owner=? AND folder=?",
                              (d["owner"], title)).fetchone()
            pid = row[0] if row else None
        pid = pid or create(con, d["owner"], title, bool(d["trashed"]))
    if d["folder"] and not project_id:
        con.execute("INSERT OR IGNORE INTO legacy_project_folders VALUES (?,?,?)", (d["owner"], d["folder"].split("/")[0], pid))
    folder = d["folder"].partition("/")[2] if not project_id else d["folder"]
    shelf = {r[0]: r[1] for r in con.execute("SELECT name,content FROM templates WHERE owner=?", (d["owner"],))}
    present = {r[0].casefold() for r in con.execute("SELECT path FROM project_files WHERE project_id=?", (pid,))}
    added: dict[str, str] = {}
    queue = [(folder, d["content"] or "")]
    while queue and len(added) < resources.REACHABLE_MAX:
        base, text = queue.pop()
        for m in resources.REF_RE.finditer(text):
            name, path = m.group(1).strip().lstrip("/"), resources.resolve(base, m.group(1))
            if name in shelf and path and path.casefold() not in present and path not in added:
                added[path] = shelf[name]
                if path.lower().endswith(".typ"):
                    queue.append((posixpath.dirname(path), shelf[name]))
    if check_quota:
        quota.check_quota(d["owner"], sum(len(c.encode("utf-8")) for c in added.values()), con)
    backups: project_files.Backups = {}
    try:
        for path, content in added.items():
            project_files.put(con, pid, project_files.clean(path, new=False), content.encode(), backups)
    except Exception:
        project_files.undo(backups)
        raise
    con.execute("UPDATE docs SET project_id=?,folder=? WHERE id=?", (pid, folder, doc_id))
    if not d["trashed"]:
        con.execute("UPDATE projects SET archived=0 WHERE id=?", (pid,))
    return pid


def payload(con, project_id: str, user: str) -> dict:
    from backend.services import project_files
    p = con.execute("SELECT * FROM projects WHERE id=? AND archived=0", (project_id,)).fetchone()
    if not p:
        raise HTTPException(404, "Project not found")
    try:
        r = role(con, project_id, user)
    except HTTPException:
        r = "guest"
    docs = [dict(d) for d in con.execute("SELECT id,title,folder FROM docs WHERE project_id=? AND trashed=0 ORDER BY folder,title", (project_id,))
            if r != "guest" or db.role_on(con, user, d["id"])]
    if r == "guest" and not docs:
        raise HTTPException(404, "Project not found")
    for d in docs:
        d["role"] = db.role_on(con, user, d["id"])
    allowed = project_files.visible(con, project_id, user)
    files = [{k: f[k] for k in ("path", "size", "version")} for f in project_files.listing(con, project_id)
             if allowed is None or f["path"] in allowed]
    folders = [x[0] for x in con.execute("SELECT path FROM project_folders WHERE project_id=? ORDER BY path", (project_id,))] if r != "guest" else []
    return {**dict(p), "role": r, "can_edit_library": r in ("owner", "editor"),
            "folders": folders, "documents": docs, "files": files}
