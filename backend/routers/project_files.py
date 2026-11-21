"""Project file routes: one file tree per project (see services/project_files.py)."""
from __future__ import annotations

import asyncio
import mimetypes
import posixpath
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from backend import db, deps
from backend.constants import UPLOAD_MAX
from backend.services import project_files, projects

router = APIRouter()


class FileMove(BaseModel):
    path: str = Field(min_length=1, max_length=240)
    target: str = Field(min_length=1, max_length=240)
    version: str = ""
    fix_refs: bool = True


@router.get("/api/projects/{project_id}/files")
def list_files(project_id: str, req: Request, user: str = Depends(deps.me)) -> dict:
    deps.limited(req, "files_list")
    con = db.connect()
    try:
        allowed = project_files.visible(con, project_id, user)
        try:
            readonly = projects.role(con, project_id, user) not in ("owner", "editor")
        except HTTPException:
            readonly = True
        files = [f for f in project_files.listing(con, project_id) if allowed is None or f["path"] in allowed]
    finally:
        con.close()
    return {"files": files, "readonly": readonly}


@router.get("/api/projects/{project_id}/files/{path:path}")
def get_file(project_id: str, path: str, req: Request, user: str = Depends(deps.me)) -> Response:
    deps.limited(req, "file_read")  # the preview loads every file; not the upload bucket
    path = project_files.clean(path, new=False)
    con = db.connect()
    try:
        allowed = project_files.visible(con, project_id, user)
        if allowed is not None and path not in allowed:
            raise HTTPException(404, "File not found")
    finally:
        con.close()
    data = project_files.read(project_id, path)
    name = posixpath.basename(path)
    mime = "application/octet-stream" if name.lower().endswith(".svg") else mimetypes.guess_type(name)[0] or "application/octet-stream"
    return Response(data, media_type=mime, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
                                                    "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


@router.put("/api/projects/{project_id}/files/{path:path}")
async def put_file(project_id: str, path: str, req: Request, version: str = "", force: bool = False, user: str = Depends(deps.me)) -> dict:
    # Text tabs autosave at the document cadence; uploads share the tight files bucket.
    deps.limited(req, "save" if project_files.is_text(path) else "files")
    data = await deps.read_capped_body(req, UPLOAD_MAX)
    return await asyncio.to_thread(project_files.write, project_id, path, data, user, version_=version or None, force=force)


@router.delete("/api/projects/{project_id}/files/{path:path}")
def delete_file(project_id: str, path: str, req: Request, version: str = "", user: str = Depends(deps.me)) -> dict:
    deps.limited(req, "files")
    return project_files.delete(project_id, path, user, version_=version)


@router.post("/api/projects/{project_id}/files-move-preview")
def move_preview(project_id: str, b: FileMove, req: Request, user: str = Depends(deps.me)) -> dict:
    deps.limited(req, "files_list")
    return project_files.move_preview(project_id, b.path, b.target, user)


@router.post("/api/projects/{project_id}/files-move")
async def move_file(project_id: str, b: FileMove, req: Request, user: str = Depends(deps.me)) -> dict:
    deps.limited(req, "rename")
    return await project_files.move(project_id, b.path, b.target, user, version_=b.version, fix_refs=b.fix_refs)
