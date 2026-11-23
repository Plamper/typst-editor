"""Server-wide font catalog and binary delivery for the browser WASM compiler."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from backend import deps
from backend.services import fonts as fonts_svc

router = APIRouter()

_FONT_ID_RE = re.compile(r"^[0-9a-f]{16}$")


@router.get("/api/fonts")
def list_fonts(req: Request, user: str = Depends(deps.me)) -> dict:
    deps.limited(req, "files_list")
    entries = fonts_svc.list_server_fonts()
    families = fonts_svc.server_font_families()
    return {
        "fonts": [
            {
                "id": f.id,
                "filename": f.filename,
                "families": list(f.families),
                "size": f.size,
                "url": f"/api/fonts/{f.id}",
            }
            for f in entries
        ],
        "families": families,
        "total_bytes": sum(f.size for f in entries),
    }


@router.get("/api/fonts/{font_id}")
def get_font(font_id: str, req: Request, user: str = Depends(deps.me)) -> FileResponse:
    deps.limited(req, "file_read")
    if not _FONT_ID_RE.fullmatch(font_id or ""):
        raise HTTPException(404, "Font not found")
    entry = fonts_svc.get_server_font(font_id)
    if not entry or not entry.path.is_file():
        raise HTTPException(404, "Font not found")
    return FileResponse(
        str(entry.path),
        media_type=entry.mime,
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )
