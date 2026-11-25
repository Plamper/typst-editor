"""Server-wide font catalog and binary delivery for the browser WASM compiler."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from backend import deps
from backend.services import fonts as fonts_svc

router = APIRouter()


@router.get("/api/fonts")
def list_fonts(req: Request, user: str = Depends(deps.me)) -> dict:
    deps.limited(req, "files_list")
    return {
        "fonts": [{"id": f.id, "families": list(f.families), "size": f.size, "url": f"/api/fonts/{f.id}"}
                  for f in fonts_svc.list_server_fonts()],
        "families": fonts_svc.server_font_families(),
    }


@router.get("/api/fonts/{font_id}")
def get_font(font_id: str, req: Request, user: str = Depends(deps.me)) -> FileResponse:
    deps.limited(req, "file_read")
    entry = fonts_svc.get_server_font(font_id)
    if not entry:
        raise HTTPException(404, "Font not found")
    return FileResponse(str(entry.path), media_type=entry.mime)
