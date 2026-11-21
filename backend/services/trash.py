"""Recoverable deletions for personal templates and project files."""
from __future__ import annotations

import sqlite3

from fastapi import HTTPException

from backend import db
from backend.services import projects, quota
from backend.services.locks import _named_lock
from backend.services.sidebar import notify_sidebar


def archive(con: sqlite3.Connection, owner: str, kind: str, name: str, content: bytes,
            project_id: str = "", doc_id: str = "", line: str = "", folder: str = "") -> str:
    """Insert one trash row and return its id (callers may need to undo
    their own insert, e.g. a failed FS delete that must keep the file)."""
    if kind not in ("personal", "file"):
        raise ValueError(f"unknown trash kind: {kind!r}")
    entry_id = db.new_id("tr_")
    con.execute("INSERT INTO trash_entries (id,owner,kind,project_id,doc_id,name,content,line,folder,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (entry_id, owner, kind, project_id, doc_id, name, content, line, folder, db.now_iso()))
    return entry_id


def restore(user: str, entry_id: str) -> dict:
    # Fixed lock order: trash lock, project files lock, DB tx last.
    from backend.services import project_files

    with _named_lock("trash:" + entry_id):
        con = db.connect()
        try:
            snap = con.execute("SELECT kind,project_id FROM trash_entries WHERE id=? AND owner=?", (entry_id, user)).fetchone()
        finally:
            con.close()
        if snap is None:
            raise HTTPException(404, "Trash entry gone")
        if snap["kind"] == "file":
            with project_files.lock(snap["project_id"]):
                result = _restore_locked(user, entry_id)
        else:
            result = _restore_locked(user, entry_id)
    # notify() -> auto() -> prepared() takes the files lock again: call it after release.
    if result["project_id"]:
        projects.notify(result["project_id"])
    return result


def _restore_locked(user: str, entry_id: str) -> dict:
    from backend.services import project_files

    backups: project_files.Backups = {}
    charged = user  # account whose quota gains the restored bytes
    try:
        with db.tx() as con:
            row = con.execute("SELECT * FROM trash_entries WHERE id=? AND owner=?", (entry_id, user)).fetchone()
            if row is None:
                raise HTTPException(404, "Trash entry gone")
            name, kind, content = row["name"], row["kind"], bytes(row["content"])
            if kind == "personal":
                # Quota-neutral: the bytes leave the trash as they land in the template.
                quota.check_quota(user, 0, con)
                con.execute("INSERT INTO templates(owner,name,content,line,folder,updated_at) VALUES (?,?,?,?,?,?)",
                            (user, name, content.decode(), row["line"], row["folder"], db.now_iso()))
            else:
                if not con.execute("SELECT 1 FROM projects WHERE id=?", (row["project_id"],)).fetchone():
                    raise HTTPException(410, "Project was deleted permanently; this entry can no longer be restored")
                projects.require(con, row["project_id"], user, edit=True)
                if con.execute("SELECT 1 FROM project_files WHERE project_id=? AND path=? COLLATE NOCASE", (row["project_id"], name)).fetchone():
                    raise HTTPException(409, "Name already exists; rename the existing file first")
                charged = project_files.owner_of(con, row["project_id"])
                project_files.put(con, row["project_id"], name, content, backups)
            con.execute("DELETE FROM trash_entries WHERE id=?", (entry_id,))
            quota.check_quota(charged, 0, con)
    except Exception as error:
        project_files.undo(backups)
        if isinstance(error, sqlite3.IntegrityError):
            raise HTTPException(409, "Name already exists; rename the existing entry first") from error
        raise
    for account in {user, charged}:
        quota.invalidate_quota_cache(account)
        notify_sidebar(account)
    return {"kind": kind, "name": name, "project_id": row["project_id"], "doc_id": ""}
