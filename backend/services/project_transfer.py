"""Explicit copies and moves of documents and files between projects."""
from __future__ import annotations

from contextlib import ExitStack

from fastapi import HTTPException

from backend import config, db, sync
from backend.services import project_files, projects, quota, resources
from backend.services.docfiles import safe_name


def transfer(source: str, target: str, kind: str, doc_id: str, name: str,
             target_name: str, move: bool, user: str, duplicate: bool = False,
             target_folder: str | None = None, lang: str = "en") -> dict:
    """kind 'document': doc_id (target_name = title, target_folder); the project files it reaches come along
    into another project unless the same bytes are already there. kind 'file': name = project path, target_name
    = path in the target project."""
    new_id = db.new_id('d_') if kind == 'document' and not move else doc_id
    folder: str | None = None
    if target_folder is not None:
        folder = (target_folder or '').strip().strip('/')
        if folder:
            folder = safe_name(folder + '/x', new=True)[:-2]
    backups: project_files.Backups = {}
    source_owner = target_owner = ''
    with ExitStack() as locks:
        for pid in sorted({source, target}):
            locks.enter_context(project_files.lock(pid))
        try:
            with db.tx() as con:
                projects.require(con, source, user, edit=move)
                projects.require(con, target, user, edit=True)
                source_owner = project_files.owner_of(con, source)
                target_owner = project_files.owner_of(con, target)
                if move and source_owner != target_owner and user != source_owner:
                    raise HTTPException(403, 'Only the source owner can move content to another owner')
                copies: dict[str, bytes] = {}
                if kind == 'document':
                    d = con.execute('SELECT * FROM docs WHERE id=? AND project_id=? AND trashed=0', (doc_id, source)).fetchone()
                    if not d:
                        raise HTTPException(404, 'Document not found')
                    dest_folder = d['folder'] if folder is None else folder
                    if duplicate:
                        number = 1
                        target_name = resources.copy_name(d['title'], number, lang=lang)
                        while con.execute('SELECT 1 FROM docs WHERE project_id=? AND folder=? AND title=? COLLATE NOCASE AND trashed=0', (target, dest_folder, target_name)).fetchone():
                            number += 1
                            target_name = resources.copy_name(d['title'], number, lang=lang)
                    title = target_name.strip()
                    if not title or len(title) > 100:
                        raise HTTPException(400, 'Invalid document name')
                    if con.execute('SELECT 1 FROM docs WHERE project_id=? AND folder=? AND title=? COLLATE NOCASE AND trashed=0 AND id<>?', (target, dest_folder, title, doc_id if move else '')).fetchone():
                        raise HTTPException(409, 'Name already exists in target folder')
                    live = sync.room_snapshot(doc_id)
                    yjs, text = live if live is not None else (d['yjs'], d['content'])
                    if source != target:
                        paths = {r[0] for r in con.execute('SELECT path FROM project_files WHERE project_id=?', (source,))}
                        for path in sorted(resources.reachable(paths, project_files.texts(con, source), d['folder'], text)):
                            data = project_files.read(source, path)
                            there = con.execute('SELECT version FROM project_files WHERE project_id=? AND path=? COLLATE NOCASE', (target, path)).fetchone()
                            if there and there[0] != project_files.version(data):
                                raise HTTPException(409, f'File name conflict in the target project: {path}')
                            if not there:
                                copies[path] = data
                    if (not move or source_owner != target_owner) and con.execute('SELECT COUNT(*) FROM docs WHERE owner=?', (target_owner,)).fetchone()[0] >= config.load().MAX_DOCS_PER_USER:
                        raise HTTPException(400, 'Too many docs')
                    now = db.now_iso()
                    if move:
                        con.execute('UPDATE docs SET project_id=?,owner=?,title=?,folder=?,content=?,yjs=?,updated_at=? WHERE id=?', (target, target_owner, title, dest_folder, text, yjs, now, doc_id))
                        if source != target:
                            con.execute('DELETE FROM invites WHERE doc_id=?', (doc_id,))
                    else:
                        con.execute('INSERT INTO docs(id,owner,title,content,folder,project_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)', (new_id, target_owner, title, text, dest_folder, target, now, now))
                else:
                    name = project_files.clean(name, new=False)
                    if duplicate:
                        number = 1
                        target_name = resources.copy_name(name, number, lang=lang)
                        while con.execute('SELECT 1 FROM project_files WHERE project_id=? AND path=? COLLATE NOCASE', (target, target_name)).fetchone():
                            number += 1
                            target_name = resources.copy_name(name, number, lang=lang)
                    target_name = project_files.clean(target_name or name)
                    if (source, name) == (target, target_name):
                        raise HTTPException(409, 'Choose another target name')
                    if con.execute('SELECT 1 FROM project_files WHERE project_id=? AND path=? COLLATE NOCASE', (target, target_name)).fetchone():
                        raise HTTPException(409, 'Name already exists in target project')
                    copies[target_name] = project_files.remove(con, source, name, backups) if move else project_files.read(source, name)
                for path, data in copies.items():
                    project_files.put(con, target, path, data, backups)
                quota.check_quota(target_owner, 0, con)
        except Exception:
            project_files.undo(backups)
            raise
    quota.invalidate_quota_cache(source_owner)
    quota.invalidate_quota_cache(target_owner)
    projects.notify(source)
    if target != source:
        projects.notify(target)
    return {'doc_id': new_id if kind == 'document' else '', 'name': target_name}
