"""Copy a personal template or a project text file, with the files it includes, into the templates or a project."""
from __future__ import annotations

import hashlib
import json
import posixpath
from collections import Counter
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend import db, deps
from backend.constants import MAX_TXT
from backend.services import project_files, projects, quota, resources
from backend.services.sidebar import notify_sidebar

router = APIRouter()


class Pull(BaseModel):
    source_scope: str
    source_project_id: str = ""
    source: str = Field(min_length=1, max_length=240)
    scope: str
    project_id: str = ""
    name: str = Field(min_length=1, max_length=240)
    folder: str = Field(default="", max_length=40)
    renames: dict[str, str] = Field(default_factory=dict)
    preview: str = ""


def source_library(con, user: str, b: Pull) -> dict[str, str]:
    if b.source_scope == "personal":
        return {r["name"]: r["content"] for r in con.execute("SELECT name,content FROM templates WHERE owner=?", (user,))}
    if b.source_scope != "project":
        raise HTTPException(400, "Choose a personal or project source")
    # A document guest may pull only files already visible through that document.
    allowed = project_files.visible(con, b.source_project_id, user)
    texts = project_files.texts(con, b.source_project_id, tuple(resources.RESOURCE_EXTS))
    return {n: c for n, c in texts.items() if allowed is None or n in allowed}


def resource_name(name: str, *, new: bool = False) -> str:
    n = project_files.clean(name, new=new)
    if not resources.kind_of(n):
        raise HTTPException(400, "Unsupported text resource extension")
    return n


def inspect_pull(con, user: str, b: Pull) -> dict:
    source = resource_name(b.source)
    shelf = source_library(con, user, b)
    if source not in shelf:
        raise HTTPException(404, "Source module not found")
    if b.scope == "personal":
        owner = user
        existing = {r["name"]: r["updated_at"] for r in con.execute("SELECT name,updated_at FROM templates WHERE owner=?", (user,))}
    elif b.scope == "project":
        if b.folder:
            raise HTTPException(400, "Include the target folder in the module path")
        projects.require(con, b.project_id, user, edit=True)
        owner = con.execute("SELECT owner FROM projects WHERE id=?", (b.project_id,)).fetchone()[0]
        existing = {r["path"]: r["version"] for r in con.execute("SELECT path,version FROM project_files WHERE project_id=?", (b.project_id,))}
    else:
        raise HTTPException(400, "Choose a personal or project target")
    root = resource_name(b.name, new=True)
    if PurePosixPath(root).suffix.lower() != PurePosixPath(source).suffix.lower():
        raise HTTPException(400, "Keep the source file extension")
    queue = [source]
    found: dict[str, str] = {}
    while queue:
        n = queue.pop(0)
        if n in found:
            continue
        if len(found) >= resources.REACHABLE_MAX:
            raise HTTPException(413, "Too many module dependencies")
        found[n] = shelf[n]
        if n.lower().endswith(".typ"):
            for m in resources.REF_RE.finditer(shelf[n]):
                resolved = resources.resolve(posixpath.dirname(n), m.group(1))
                if resolved in shelf and resolved not in found:
                    queue.append(resolved)
    names = {source: root}
    for n in found:
        if n == source:
            continue
        relative = posixpath.relpath(n, posixpath.dirname(source) or ".")
        mapped = posixpath.normpath(posixpath.join(posixpath.dirname(root), relative))
        if mapped.startswith("../") or mapped == "..":
            mapped = n
        names[n] = resource_name(b.renames.get(n, posixpath.basename(n) if b.scope == "personal" else mapped), new=True)
    if any(PurePosixPath(names[n]).suffix.lower() != PurePosixPath(n).suffix.lower() for n in names):
        raise HTTPException(400, "Keep dependency file extensions")
    if b.scope == "personal" and any("/" in n or len(n) > 100 or resources.res_name(n) != n for n in names.values()):
        raise HTTPException(400, "Personal modules need filenames; choose a separate collection folder")
    duplicate_names = {n for n, count in Counter(names.values()).items() if count > 1}
    copies = {}
    for old, content in found.items():
        def rewrite(match, old=old):
            resolved = resources.resolve(posixpath.dirname(old), match.group(1))
            if resolved not in names:
                return match.group(0)
            target = names[resolved]
            ref = "/" + target if match.group(1).startswith("/") else posixpath.relpath(target, posixpath.dirname(names[old]) or ".")
            return json.dumps(ref)
        copies[names[old]] = resources.REF_RE.sub(rewrite, content) if old.lower().endswith(".typ") else content
        if len(copies[names[old]].encode("utf-8")) > MAX_TXT:
            raise HTTPException(413, "Text resource exceeds 200 KB")
    # #7: case-variant module names ('Layout.typ' vs 'layout.typ') would show
    # as two rows, so the collision set folds case.
    existing_lower = {n.lower() for n in existing}
    collisions = sorted(
        (set(copies) & existing.keys()) | duplicate_names
        | {n for n in copies if n.lower() in existing_lower}
    )
    fingerprint = [b.model_dump(exclude={"preview"}), copies, {n: existing.get(n) for n in copies}]
    return {"owner": owner, "content": copies, "name": root,
            "files": [{"source": n, "name": names[n]} for n in found],
            "dependencies": [names[n] for n in found if n != source], "collisions": collisions,
            "warnings": ["Only static text dependencies can be pulled. Check dynamic paths and binary assets manually."],
            "preview": hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()}


@router.post("/api/library/pull-preview")
def pull_preview(b: Pull, req: Request, user: str = Depends(deps.me)) -> dict:
    deps.limited(req, "files_list")
    con = db.connect()
    try:
        p = inspect_pull(con, user, b)
        return {k: v for k, v in p.items() if k not in ("content", "owner")}
    finally:
        con.close()


@router.post("/api/library/pull")
def pull_module(b: Pull, req: Request, user: str = Depends(deps.me)) -> dict:
    deps.limited(req, "templates")
    with db.tx() as con:
        p = inspect_pull(con, user, b)
        if p["collisions"]:
            raise HTTPException(409, "Name already exists; choose another target name")
        if b.preview != p["preview"]:
            raise HTTPException(409, "Source or target changed; review the pull again")
        quota.check_quota(p["owner"], sum(len(c.encode("utf-8")) for c in p["content"].values()), con)
        now = db.now_iso()
        backups: project_files.Backups = {}
        try:
            for name, content in p["content"].items():
                if b.scope == "personal":
                    con.execute("INSERT INTO templates(owner,name,content,line,folder,updated_at) VALUES (?,?,?,?,?,?)",
                                (user, name, content, resources.insert_line(name), b.folder, now))
                else:
                    project_files.put(con, b.project_id, name, content.encode(), backups)
        except Exception:
            project_files.undo(backups)
            raise
        if b.scope == "personal":
            deps.ensure_folder(con, user, "tpl", b.folder)
    quota.invalidate_quota_cache(p["owner"])
    if b.scope == "project":
        projects.notify(b.project_id)
    else:
        notify_sidebar(user)
    return {"name": p["name"], "version": now, "files": p["files"]}
