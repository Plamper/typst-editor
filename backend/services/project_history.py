"""Project checkpoints include document sources and every project file."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import posixpath
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from fastapi import HTTPException

from backend import db, sync
from backend.constants import AUTO_SNAP_KEEP, AUTO_SNAP_KEEP_WEEKS
from backend.services import quota
from backend.services.docfiles import store

log = logging.getLogger("typst.main")


def stamp(con: sqlite3.Connection, project_id: str) -> tuple:
    docs = tuple(tuple(r) for r in con.execute("SELECT id,owner,title,folder,content,yjs,trashed,updated_at FROM docs WHERE project_id=? ORDER BY id", (project_id,)))
    files = tuple(tuple(r) for r in con.execute("SELECT path,version FROM project_files WHERE project_id=? ORDER BY path", (project_id,)))
    return docs, files


def unchanged(con: sqlite3.Connection, project_id: str, expected: tuple) -> None:
    if stamp(con, project_id) != expected:
        raise HTTPException(409, "Project changed; retry checkpoint operation")


@contextmanager
def prepared(project_id: str, user: str) -> Iterator[tuple[dict, tuple]]:
    from backend.services import project_files, projects

    con = db.connect()
    try:
        projects.require(con, project_id, user)
    finally:
        con.close()
    with project_files.lock(project_id):
        con = db.connect()
        try:
            con.execute("BEGIN")
            projects.require(con, project_id, user)
            expected = stamp(con, project_id)
            state = capture(con, project_id)
        finally:
            con.close()
        yield state, expected


def capture(con: sqlite3.Connection, project_id: str) -> dict:
    documents = []
    for row in con.execute("SELECT id,title,folder,content FROM docs WHERE project_id=? AND trashed=0 ORDER BY id", (project_id,)):
        doc = dict(row)
        live = sync.room_text(doc['id'])
        if live is not None:
            doc['content'] = live
        documents.append(doc)
    files = {r[0]: base64.b64encode(store.get(project_id, r[0])).decode('ascii')
             for r in con.execute("SELECT path FROM project_files WHERE project_id=? ORDER BY path", (project_id,))}
    return {'documents': documents, 'files': files}


def load(content: str) -> dict:
    """A stored checkpoint; the format before project files ({documents: [{..., files}], resources}) is converted
    with the rules of migration v34 (attachments below their document's folder, references kept working)."""
    from backend.services import project_files as pf

    state = json.loads(content)
    if 'resources' not in state:
        return state
    modules = state.pop('resources')
    for d in state['documents']:
        try:
            d['folder'] = pf.doc_folder(d.get('folder'))
        except ValueError:
            d['folder'] = ''
    module_map, doc_maps = pf.legacy_paths([], list(modules), [(d['id'], d.get('folder') or '', list(d.get('files') or {})) for d in state['documents']])
    files = {}
    for name, new in module_map.items():
        text = modules[name]
        files[new] = base64.b64encode(pf.legacy_refs(text, posixpath.dirname(name), posixpath.dirname(new), {}, module_map).encode()).decode('ascii')
    for d in state['documents']:
        local = doc_maps[d['id']]
        for old, encoded in (d.pop('files', None) or {}).items():
            new = local[old]
            if new.lower().endswith('.typ'):
                text = base64.b64decode(encoded).decode('utf-8', errors='surrogateescape')
                fixed = pf.legacy_refs(text, posixpath.dirname(old), posixpath.dirname(new), local, module_map)
                encoded = base64.b64encode(fixed.encode('utf-8', errors='surrogateescape')).decode('ascii')
            files[new] = encoded
        d['content'] = pf.legacy_refs(d['content'], '', d.get('folder') or '', local, module_map)
    state['files'] = files
    return state


def _retain(con: sqlite3.Connection, project_id: str) -> None:
    """Retention for automatic checkpoints (runs inside every save tx).

    Each checkpoint embeds document contents and all project files base64 - pruning a checkpoint frees those bytes for real
    (asset bytes are per-checkpoint copies, not deduped). Policy: keep the
    newest AUTO_SNAP_KEEP automatic checkpoints, plus one per older week
    within AUTO_SNAP_KEEP_WEEKS weeks. Manual checkpoints (kind='manual')
    and the project's oldest row (creation anchor) are never removed.
    """
    rows = con.execute(
        "SELECT id,kind,created_at FROM project_snapshots WHERE project_id=? "
        "ORDER BY created_at DESC,rowid DESC", (project_id,)).fetchall()
    if len(rows) <= AUTO_SNAP_KEEP:
        return  # autos alone are within the flat cap; nothing prunable
    autos: list[tuple[str, str]] = [(r["id"], r["created_at"]) for r in rows if r["kind"] == "auto"]
    # Manual checkpoints and the project's oldest row (creation anchor)
    # never enter the delete set, whatever the auto backlog looks like.
    keep = {r["id"] for r in rows if r["kind"] == "manual"}
    keep.add(rows[-1]["id"])
    keep.update(sid for sid, _created in autos[:AUTO_SNAP_KEEP])
    seen_weeks: set[int] = set()
    now = datetime.now(UTC)
    for sid, created in autos[AUTO_SNAP_KEEP:]:
        week = (now - datetime.fromisoformat(created)).days // 7
        if week < AUTO_SNAP_KEEP_WEEKS and week not in seen_weeks:
            seen_weeks.add(week)
            keep.add(sid)
    con.execute(
        "DELETE FROM project_snapshots WHERE project_id=? AND id NOT IN "
        f"({','.join('?' * len(keep))})", (project_id, *keep))


def save(con: sqlite3.Connection, project_id: str, state: dict, label: str = '', kind: str = 'manual', author: str = '', check: bool = True) -> str:
    content = json.dumps(state, ensure_ascii=False, sort_keys=True)
    owner = con.execute('SELECT owner FROM projects WHERE id=?', (project_id,)).fetchone()[0]
    sid = db.new_id('ps_')
    con.execute('INSERT INTO project_snapshots (id,project_id,content,label,created_at,kind,author) VALUES (?,?,?,?,?,?,?)',
                (sid, project_id, content, label, db.now_iso(), kind, author))
    _retain(con, project_id)
    if check:  # a restore checks the final state itself (it may free space)
        quota.check_quota(owner, 0, con)
    return sid


def _readable(data: bytes) -> str:
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        return f"Binary: {len(data)} bytes, SHA256 {hashlib.sha256(data).hexdigest()}"


def diff_files(state: dict) -> dict[str, str]:
    files = {f"{d['title']} [{d['id']}].typ": d['content'] for d in state['documents']}
    files.update({path: _readable(base64.b64decode(encoded)) for path, encoded in state['files'].items()})
    return files


def diff_entries(state: dict) -> dict[str, dict]:
    """Per entry: display path as shown in the sidebar (no project name), text or binary marker."""
    out: dict[str, dict] = {}

    def put(key: str, name: str, data: bytes | str) -> None:
        if isinstance(data, str):
            out[key] = {"name": name, "text": data, "binary": False, "size": len(data.encode()), "sig": data}
            return
        try:
            out[key] = {"name": name, "text": data.decode("utf-8"), "binary": False, "size": len(data), "sig": data.decode("utf-8")}
        except UnicodeDecodeError:
            out[key] = {"name": name, "text": "", "binary": True, "size": len(data), "sig": hashlib.sha256(data).hexdigest()}

    for d in state["documents"]:
        folder = (d.get("folder") or "").strip("/")
        put(f"d:{d['id']}", (folder + "/" if folder else "") + d["title"] + ".typ", d["content"])
    for path, encoded in state["files"].items():
        put(f"f:{path}", path, base64.b64decode(encoded))
    return out


def changes(old: dict, new: dict) -> list[dict]:
    """File-level changes between two captured states, sorted by display path."""
    a, b = diff_entries(old), diff_entries(new)
    rows = []
    for key in a.keys() | b.keys():
        x, y = a.get(key), b.get(key)
        if x and y and x["sig"] == y["sig"] and x["name"] == y["name"]:
            continue
        entry = y or x
        assert entry is not None
        rows.append({"name": entry["name"], "status": "added" if not x else "deleted" if not y else "modified",
                     "binary": bool((x and x["binary"]) or (y and y["binary"])),
                     "before": x["text"] if x else "", "after": y["text"] if y else "",
                     "before_size": x["size"] if x else 0, "after_size": y["size"] if y else 0})
    return sorted(rows, key=lambda r: (r["name"].casefold(), r["name"]))


def diff_text(state: dict) -> str:
    return '\n\n'.join(f'=== {name} ===\n{content}' for name, content in sorted(diff_files(state).items()))


def auto(project_id: str, present: frozenset[str] = frozenset()) -> None:
    from datetime import UTC, datetime, timedelta

    from backend.constants import SNAP_EVERY

    try:
        con = db.connect()
        try:
            p = con.execute('SELECT owner,archived FROM projects WHERE id=?', (project_id,)).fetchone()
            if not p or p['archived']:
                return
            last = con.execute('SELECT created_at FROM project_snapshots WHERE project_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (project_id,)).fetchone()
            if last and last[0] >= (datetime.now(UTC) - timedelta(seconds=SNAP_EVERY)).isoformat():
                return
        finally:
            con.close()
        with prepared(project_id, p['owner']) as (state, expected), db.tx() as con:
            from backend.services import projects

            # The owner only authorises the capture; the actor is whoever
            # last wrote through a route (note_editor), not the owner.
            projects.require(con, project_id, p['owner'], edit=True, note=False)
            last = con.execute('SELECT created_at FROM project_snapshots WHERE project_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (project_id,)).fetchone()
            if last and last[0] >= (datetime.now(UTC) - timedelta(seconds=SNAP_EVERY)).isoformat():
                return
            unchanged(con, project_id, expected)
            save(con, project_id, state, kind='auto')
        quota.invalidate_quota_cache(p['owner'])
        from backend.services import notifications
        notifications.notify_changes(project_id, present)
    except (HTTPException, sqlite3.Error, OSError) as e:
        log.warning('Project auto checkpoint skipped: %s', e)


def auto_document(doc_id: str, present: frozenset[str] = frozenset(), actor: str = "") -> None:
    # Warn-only: callers (save_doc / op_edit / files routes) have already
    # committed their mutation and only catch HTTPException, so a busy DB
    # here must not turn into a post-commit 500.
    try:
        con = db.connect()
        try:
            row = con.execute('SELECT project_id FROM docs WHERE id=?', (doc_id,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:
        log.warning('auto_document %s lookup skipped: %s', doc_id, e)
        return
    if row and row[0]:
        if actor:
            from backend.services import notifications

            notifications.note_editor(row[0], actor)
        auto(row[0], present)
