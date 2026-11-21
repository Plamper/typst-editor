"""Source ZIP import: validate first, then publish documents and project files at their ZIP paths."""
from __future__ import annotations

import io
import json
import posixpath
import sqlite3
import stat
import zipfile
from pathlib import PurePosixPath

from fastapi import HTTPException

from backend import config, db, deps
from backend.constants import EXPORT_MAX, MAX_TXT, TITLE_MAX, UPLOAD_MAX
from backend.services import project_files, projects, quota, resources
from backend.services.docfiles import safe_name
from backend.services.sidebar import notify_sidebar


def _entries(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    entries = [e for e in archive.infolist() if not e.is_dir()]
    if len(entries) > config.load().MAX_FILES_PER_DOC + 1:
        raise HTTPException(400, "Too many files in ZIP")
    if sum(e.file_size for e in entries) > EXPORT_MAX:
        raise HTTPException(413, "Uncompressed ZIP too large (max 100 MB)")
    return entries


def _typ_texts(archive: zipfile.ZipFile) -> dict[str, str]:
    out = {}
    for e in _entries(archive):
        if e.filename.lower().endswith(".typ") and e.file_size <= MAX_TXT:
            with archive.open(e) as stream:
                out[e.filename] = stream.read(MAX_TXT + 1).decode("utf-8", errors="replace")
    return out


def zip_preview(data: bytes) -> dict:
    """What the import dialog offers: every .typ file (the ones nobody includes ticked, the others with their
    first includer) and the names of all other files, which become project files."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            texts = _typ_texts(archive)
            others = sorted(e.filename for e in _entries(archive) if e.filename not in texts)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, EOFError) as e:
        raise HTTPException(400, "Invalid or unsupported ZIP") from e
    included_by: dict[str, str] = {}
    for name, text in sorted(texts.items()):
        for m in resources.REF_RE.finditer(text):
            target = resources.resolve(posixpath.dirname(name), m.group(1))
            if target in texts and target != name:
                included_by.setdefault(target, name)
    return {"entries": sorted(texts), "suggested": sorted(n for n in texts if n not in included_by),
            "included_by": included_by, "others": others}


def listed(raw: str) -> list[str]:
    """The `documents` form field: a JSON list of ZIP entry paths (may be empty)."""
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        value = None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise HTTPException(400, "documents must be a JSON list of ZIP paths")
    return value


def read_project_zip(data: bytes) -> list[tuple[str, bytes]]:
    """Every file of the ZIP at its path, validated (paths, count, sizes, UTF-8 texts)."""
    if len(data) > UPLOAD_MAX:
        raise HTTPException(413, "ZIP too large (max 10 MB)")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files, seen = [], set()
            actual_total = 0
            for item in _entries(archive):
                name = item.filename
                if name.startswith("/") or "\\" in name or ".." in PurePosixPath(name).parts or stat.S_ISLNK(item.external_attr >> 16):
                    raise HTTPException(400, "Unsafe ZIP path")
                if not name or safe_name(name) != name or name.casefold() in seen:
                    raise HTTPException(400, "Invalid or duplicate ZIP path")
                seen.add(name.casefold())
                text = PurePosixPath(name).suffix.lower() in deps.TEXT_SUFFIX
                limit = MAX_TXT if text else UPLOAD_MAX
                if item.file_size > limit:
                    raise HTTPException(413, "File in ZIP too large")
                with archive.open(item) as stream:
                    content = stream.read(limit + 1)
                actual_total += len(content)
                if len(content) > limit or actual_total > EXPORT_MAX:
                    raise HTTPException(413, "Uncompressed ZIP data too large")
                if text:
                    try:
                        content = content.decode("utf-8-sig").encode("utf-8")
                    except UnicodeDecodeError as e:
                        raise HTTPException(400, "Text files in ZIP must be UTF-8") from e
                files.append((name, content))
            return files
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, EOFError) as e:
        raise HTTPException(400, "Invalid or unsupported ZIP") from e


def import_project_zip(user: str, data: bytes, documents: list[str], *, folder: str = "", project_id: str | None = None, name: str = "") -> dict:
    """Every ZIP entry keeps its path (below `folder` inside an existing project). Listed .typ entries become
    documents titled by their stem in their ZIP folder, the rest project files. Without project_id a new
    project named `name` (the ZIP file name without .zip) is created."""
    from backend.routers.project_folders import folder_path

    folder = folder_path(folder) if folder.strip("/") else ""
    files = read_project_zip(data)
    names = {n for n, _ in files}
    if any(d not in names or not d.lower().endswith(".typ") for d in documents):
        raise HTTPException(400, "A chosen document is not a .typ file of the ZIP")
    docs = [(posixpath.join(folder, posixpath.dirname(n)).strip("/"), PurePosixPath(n).stem, content.decode("utf-8")) for n, content in files if n in documents]
    if any(not title or len(title) > TITLE_MAX for _, title, _ in docs):
        raise HTTPException(400, f"Title: 1-{TITLE_MAX} chars")
    backups: project_files.Backups = {}
    created: list[str] = []
    try:
        # A new project's id is unknown yet, but nobody else can write to it either.
        with project_files.lock(project_id or ""), db.tx() as con:
            if project_id:
                projects.require(con, project_id, user, edit=True)
                pid = project_id
            else:
                title = (name.strip().removesuffix(".zip").removesuffix(".ZIP").strip() or "Import")[:TITLE_MAX]
                pid = projects.create(con, user, title)
            owner = project_files.owner_of(con, pid)
            if con.execute("SELECT COUNT(*) FROM docs WHERE owner=?", (owner,)).fetchone()[0] + len(docs) > config.load().MAX_DOCS_PER_USER:
                raise HTTPException(400, "Too many docs")
            now = db.now_iso()
            for doc_folder, title, text in docs:
                if con.execute("SELECT 1 FROM docs WHERE project_id=? AND folder=? AND title=? COLLATE NOCASE AND trashed=0", (pid, doc_folder, title)).fetchone():
                    raise HTTPException(409, f"Document title already exists in this folder: {title}")
                did = db.new_id("d_")
                con.execute("INSERT INTO docs(id,owner,title,content,folder,project_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                            (did, owner, title, text, doc_folder, pid, now, now))
                created.append(did)
            for n, content in files:
                if n in documents:
                    continue
                path = project_files.clean(posixpath.join(folder, n), new=False)
                if con.execute("SELECT 1 FROM project_files WHERE project_id=? AND path=? COLLATE NOCASE", (pid, path)).fetchone():
                    raise HTTPException(409, f"Name already exists in this project: {path}")
                project_files.put(con, pid, path, content, backups)
            quota.check_quota(owner, 0, con)
    except sqlite3.IntegrityError as e:
        project_files.undo(backups)
        raise HTTPException(409, "Document title already exists") from e
    except sqlite3.OperationalError as e:
        project_files.undo(backups)
        raise deps.busy_503("import_project_zip", e) from e
    except Exception:
        project_files.undo(backups)
        raise
    quota.invalidate_quota_cache(owner)
    projects.notify(pid)
    notify_sidebar(user)
    return {"project_id": pid, "documents": created, "id": created[0] if created else "", "files": len(files) - len(docs)}
