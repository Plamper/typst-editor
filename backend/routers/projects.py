"""Projects: listing, membership, documents, conversion and source ZIPs."""
from __future__ import annotations

import asyncio
import shutil
import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from backend import config, db, deps, sync
from backend.constants import MAX_TXT
from backend.schemas import Share
from backend.services import project_files, projects, quota
from backend.services.docfiles import safe_name
from backend.services.sidebar import notify_sidebar

router = APIRouter()

class ProjectDoc(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    folder: str = Field(default="", max_length=240)
    content: str = Field(default="", max_length=200001)

@router.get("/api/projects")
def list_projects(req: Request, user: str = Depends(deps.me)) -> dict:
    deps.limited(req, "sidebar")
    con = db.connect()
    try:
        ids = con.execute("SELECT DISTINCT p.id FROM projects p LEFT JOIN project_members m ON m.project_id=p.id AND m.username=? LEFT JOIN docs d ON d.project_id=p.id LEFT JOIN shares s ON s.doc_id=d.id AND s.username=? WHERE p.archived=0 AND (p.owner=? OR m.username IS NOT NULL OR (s.username IS NOT NULL AND d.trashed=0)) ORDER BY p.title", (user, user, user)).fetchall()
        return {"projects": [projects.payload(con, r[0], user) for r in ids]}
    finally:
        con.close()

@router.get("/api/projects/{project_id}")
def get_project(project_id: str, user: str = Depends(deps.me)) -> dict:
    con = db.connect()
    try:
        p = projects.payload(con, project_id, user)
        p["members"] = ([dict(x) for x in con.execute("SELECT username,role FROM project_members WHERE project_id=?", (project_id,))] if p["role"] != "guest" else [])
        return p
    finally:
        con.close()


@router.post("/api/projects/{project_id}/members")
async def set_member(project_id: str, b: Share, req: Request, user: str = Depends(deps.me)) -> dict:
    deps.limited(req, "share")
    if b.role not in ("editor", "reviewer"):
        raise HTTPException(400, "Role must be editor or reviewer")
    with db.tx() as con:
        projects.require(con, project_id, user, owner=True)
        if b.username == user or not con.execute("SELECT 1 FROM users WHERE name=?", (b.username,)).fetchone():
            raise HTTPException(400, "Invalid member")
        con.execute("INSERT INTO project_members VALUES (?,?,?) ON CONFLICT(project_id,username) DO UPDATE SET role=excluded.role",
                    (project_id, b.username, b.role))
        ids = [r[0] for r in con.execute("SELECT id FROM docs WHERE project_id=?", (project_id,))]
    sync.drop_role_cache(b.username)
    for did in ids:
        await sync.kick_user(did, b.username)
    projects.notify(project_id)
    return {"ok": True}


@router.delete("/api/projects/{project_id}/members/{username}")
async def remove_member(project_id: str, username: str, req: Request, user: str = Depends(deps.me)) -> dict:
    deps.limited(req, "share")
    with db.tx() as con:
        projects.require(con, project_id, user, owner=True)
        removed = con.execute("DELETE FROM project_members WHERE project_id=? AND username=?", (project_id, username))
        if not removed.rowcount:
            raise HTTPException(404, "Not a project member")
        ids = [r[0] for r in con.execute("SELECT id FROM docs WHERE project_id=?", (project_id,))]
        # Removing a member revokes their access throughout this project.
        for did in ids:
            con.execute("DELETE FROM shares WHERE doc_id=? AND username=?", (did, username))
            con.execute("DELETE FROM invites WHERE doc_id=?", (did,))
    sync.drop_role_cache(username)
    for did in ids:
        await sync.kick_user(did, username)
    notify_sidebar(username)
    projects.notify(project_id)
    return {"ok": True}


@router.post("/api/projects/{project_id}/docs")
def create_document(project_id: str, b: ProjectDoc, req: Request, user: str = Depends(deps.me)) -> dict:
    deps.limited(req, "create")
    folder = safe_name(b.folder + "/placeholder.typ", new=True).rsplit("/", 1)[0] if b.folder else ""
    if not b.title.strip() or len(b.content.encode("utf-8")) > MAX_TXT:
        raise HTTPException(400, "Invalid title or content too large")
    with db.tx() as con:
        projects.require(con, project_id, user, edit=True)
        owner = con.execute("SELECT owner FROM projects WHERE id=?", (project_id,)).fetchone()[0]
        quota.check_quota(owner, len(b.content.encode("utf-8")), con)
        if con.execute("SELECT COUNT(*) FROM docs WHERE owner=?", (owner,)).fetchone()[0] >= config.load().MAX_DOCS_PER_USER:
            raise HTTPException(400, "Too many docs")
        if con.execute("SELECT 1 FROM docs WHERE project_id=? AND folder=? AND title=? COLLATE NOCASE AND trashed=0", (project_id, folder, b.title.strip())).fetchone():
            raise HTTPException(409, "Document title already exists in this folder")
        did, now = db.new_id("d_"), db.now_iso()
        try:
            con.execute("INSERT INTO docs(id,owner,title,content,folder,project_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                        (did, owner, b.title.strip(), b.content, folder, project_id, now, now))
        except sqlite3.IntegrityError as e:
            raise HTTPException(409, "Document title already exists") from e
        projects.attach_doc(con, did, project_id)
    quota.invalidate_quota_cache(owner)
    projects.notify(project_id)
    return {"id": did}


@router.delete("/api/projects/{project_id}")
async def trash_project_documents(project_id: str, req: Request, user: str = Depends(deps.me)) -> dict:
    deps.limited(req, "delete")
    with db.tx() as con:
        projects.require(con, project_id, user, owner=True)
        ids = [r[0] for r in con.execute("SELECT id FROM docs WHERE project_id=? AND trashed=0", (project_id,))]
        con.execute("UPDATE docs SET trashed=1,updated_at=? WHERE project_id=?", (db.now_iso(), project_id))
        con.execute("UPDATE projects SET archived=1 WHERE id=?", (project_id,))
        # Keep the library and membership while trashed documents can be restored.
        if not con.execute("SELECT 1 FROM docs WHERE project_id=?", (project_id,)).fetchone():
            # Hard-deleted project: no document can pull its trash back, so
            # archived resource copies could never be restored again.
            con.execute("DELETE FROM trash_entries WHERE project_id=?", (project_id,))
            con.execute("DELETE FROM projects WHERE id=?", (project_id,))
            shutil.rmtree(deps.get_files_dir() / project_id, ignore_errors=True)
    for did in ids:
        await sync.drop(did)
    projects.notify(project_id)
    notify_sidebar(user)
    return {"ok": True, "trashed": len(ids)}


class ProjectTitle(BaseModel):
    title: str = Field(min_length=1, max_length=100)


@router.post("/api/projects")
def create_project(b: ProjectTitle, req: Request, user: str = Depends(deps.me)) -> dict:
    deps.limited(req, "create")
    if not b.title.strip():
        raise HTTPException(400, "Title is required")
    with db.tx() as con:
        pid = projects.create(con, user, b.title.strip())
    projects.notify(pid)
    return {"id": pid}


@router.post("/api/projects/{project_id}/rename")
def rename_project(project_id: str, b: ProjectTitle, req: Request, user: str = Depends(deps.me)) -> dict:
    deps.limited(req, "move")
    if not b.title.strip():
        raise HTTPException(400, "Title is required")
    with db.tx() as con:
        projects.require(con, project_id, user, owner=True)
        if con.execute("SELECT 1 FROM projects WHERE owner=? AND title=? COLLATE NOCASE AND id<>?", (user, b.title.strip(), project_id)).fetchone():
            raise HTTPException(409, "Project title already exists")
        con.execute("UPDATE projects SET title=? WHERE id=?", (b.title.strip(), project_id))
    projects.notify(project_id)
    return {"ok": True}


class TransferEntry(BaseModel):
    target_project_id: str
    kind: Literal['document', 'file']  # file: name = project path
    doc_id: str = ''
    name: str = ''
    target_name: str = Field(default='', max_length=240)
    target_folder: str | None = None
    move: bool = False
    duplicate: bool = False


@router.post('/api/projects/{project_id}/transfer')
async def transfer_entry(project_id: str, b: TransferEntry, req: Request, user: str = Depends(deps.me)) -> dict:
    from backend.services.project_transfer import transfer
    from backend.services.sidebar import doc_users

    deps.limited(req, 'move')
    if b.duplicate and (b.move or b.target_project_id != project_id):
        raise HTTPException(400, 'Duplicate stays in the source project')
    audience = doc_users(b.doc_id) if b.kind == 'document' else []
    result = await asyncio.to_thread(transfer, project_id, b.target_project_id, b.kind, b.doc_id, b.name, b.target_name, b.move, user, b.duplicate, b.target_folder, deps.req_lang(req))
    if b.move and b.kind == 'document':
        for username in audience:
            sync.drop_role_cache(username)
            if not db.doc_role(username, b.doc_id):
                await sync.kick_user(b.doc_id, username)
            notify_sidebar(username)
    return result


class ConvertEntry(BaseModel):
    doc_id: str = Field(default='', max_length=100)  # this document becomes <folder>/<Title>.typ
    path: str = Field(default='', max_length=240)  # or this .typ file becomes a document


@router.post('/api/projects/{project_id}/convert')
async def convert_entry(project_id: str, b: ConvertEntry, req: Request, user: str = Depends(deps.me)) -> dict:
    """A document becomes a .typ project file or a .typ file a document; the old entry goes to the trash.
    `broken` lists the texts that still quote a converted file (documents cannot be included)."""
    deps.limited(req, 'move')
    if bool(b.doc_id) == bool(b.path):
        raise HTTPException(400, 'Convert either a document or a file')
    if b.doc_id:
        deps.check_doc_id(b.doc_id)
        return await project_files.doc_to_file(project_id, b.doc_id, user)
    return await asyncio.to_thread(project_files.file_to_doc, project_id, b.path, user)


@router.post('/api/projects/{project_id}/import-zip')
def import_project_source(project_id: str, f: UploadFile, req: Request, folder: str = Form(''), documents: str = Form('[]'), user: str = Depends(deps.me)) -> dict:
    from backend.constants import UPLOAD_MAX
    from backend.services.projectzip import import_project_zip, listed

    deps.limited(req, 'create')
    data = f.file.read(UPLOAD_MAX + 1)
    return import_project_zip(user, data, listed(documents), folder=folder, project_id=project_id)


@router.get('/api/projects/{project_id}/export-zip')
def export_project_zip(project_id: str, req: Request, user: str = Depends(deps.me)):
    """The whole tree: documents as <folder>/<Title>.typ next to every project file the user may see."""
    import io
    import posixpath
    import zipfile

    from fastapi.responses import Response

    from backend import render
    from backend.constants import EXPORT_MAX

    deps.limited(req, 'export_doc', user)
    con = db.connect()
    try:
        project = projects.payload(con, project_id, user)
        texts = {d['id']: project_files.doc_text(d['id'], d['content']) for d in con.execute(
            'SELECT id,content FROM docs WHERE project_id=? AND trashed=0', (project_id,))}
    finally:
        con.close()
    output = io.BytesIO()
    total, seen, skipped = 0, set(), []
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        def write(name: str, content: bytes) -> None:
            nonlocal total
            if name.casefold() in seen:
                skipped.append(name)
                return
            seen.add(name.casefold())
            total += len(content)
            if total > EXPORT_MAX:
                raise HTTPException(413, 'Project export too large (max 100 MB)')
            archive.writestr(name, content)

        for doc in project['documents']:
            write(posixpath.join(doc['folder'], render._export_stem(doc['title']) + '.typ'), texts[doc['id']].encode())
        for f in project['files']:
            write(f['path'], project_files.read(project_id, f['path']))
        if project['role'] != 'guest':  # the history holds every file, guests may only see some
            from backend.services import project_git
            for name, content in project_git.export_files(project_id).items():
                write(name, content)
        if skipped:
            archive.writestr('SKIPPED.txt', 'Files left out (same name as another file):\n' + '\n'.join(skipped) + '\n')
    return Response(output.getvalue(), media_type='application/zip', headers={'Content-Disposition': f'attachment; filename="project-{project_id}.zip"', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
