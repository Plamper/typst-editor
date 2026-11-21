"""File storage and path rules for project files (services/project_files.py).

Disk layout: <files>/<key>/<path>, the key being the project id and <path>
a relative "a/b/c.png" style path.
"""
from __future__ import annotations

import abc
import logging
import os
import re
import secrets
from pathlib import Path
from typing import NamedTuple

from fastapi import HTTPException

from backend import deps
from backend.constants import MAX_FOLDER_DEPTH

log = logging.getLogger("typst.main")


class FileEntry(NamedTuple):
    path: str  # relative posix path inside the tree ("a.png", "sub/b.typ")
    size: int
    mtime: float


class FileStore(abc.ABC):
    """Layout + atomic writes for project files. Local disk for now;
    a later object store only replaces this interface."""

    @abc.abstractmethod
    def doc_path(self, doc_id: str, path: str) -> Path:
        """Final on-disk location (inside the store, traversal-safe)."""

    @abc.abstractmethod
    def tmp_path(self, doc_id: str, path: str) -> Path:
        """Fresh hidden tmp sibling for atomic upload (never listed/counted)."""

    @abc.abstractmethod
    def publish(self, tmp: Path, doc_id: str, path: str) -> None:
        """mkdir parents + atomic replace tmp -> final."""

    @abc.abstractmethod
    def put(self, doc_id: str, path: str, data: bytes) -> int:
        """Atomic small write (text saves). Returns the size."""

    @abc.abstractmethod
    def get(self, doc_id: str, path: str) -> bytes:
        """Raises FileNotFoundError when gone."""

    @abc.abstractmethod
    def delete(self, doc_id: str, path: str) -> bool:
        """True when something was removed."""

    @abc.abstractmethod
    def list(self, doc_id: str) -> list[FileEntry]:
        """Recursive, sorted by path; skips hidden and *.tmp.* files."""

    @abc.abstractmethod
    def stat(self, doc_id: str, path: str) -> FileEntry | None:
        ...


class LocalFileStore(FileStore):
    def __init__(self, base: Path | None = None) -> None:
        self._base = base

    def _root(self) -> Path:
        if self._base is not None:
            return self._base
        return deps.get_files_dir()

    def doc_path(self, doc_id: str, path: str) -> Path:
        root = (self._root() / doc_id).resolve()
        base = self._root().resolve()
        if root != base and base not in root.parents:
            raise HTTPException(400, "Bad doc")
        p = (root / Path(path)).resolve()
        if p != root and root not in p.parents:
            raise HTTPException(400, "Bad file name")
        return p

    def tmp_path(self, doc_id: str, path: str) -> Path:
        final = self.doc_path(doc_id, path)
        return final.parent / f"{final.name}.tmp.{secrets.token_hex(8)}"

    def publish(self, tmp: Path, doc_id: str, path: str) -> None:
        final = self.doc_path(doc_id, path)
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, final)

    def put(self, doc_id: str, path: str, data: bytes) -> int:
        tmp = self.tmp_path(doc_id, path)
        try:
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
            self.publish(tmp, doc_id, path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return len(data)

    def get(self, doc_id: str, path: str) -> bytes:
        p = self.doc_path(doc_id, path)
        if not p.is_file():
            raise FileNotFoundError(path)
        return p.read_bytes()

    def delete(self, doc_id: str, path: str) -> bool:
        p = self.doc_path(doc_id, path)
        try:
            p.unlink()
            return True
        except FileNotFoundError:
            return False

    def list(self, doc_id: str) -> list[FileEntry]:
        d = self._root() / doc_id
        out: list[FileEntry] = []
        if not d.is_dir():
            return out
        try:
            walking = list(d.rglob("*"))
        except OSError:
            return out
        for p in walking:
            try:
                if p.is_symlink() or not p.is_file():
                    continue
                rel = p.relative_to(d).as_posix()
                if any(seg.startswith(".") for seg in rel.split("/")) or ".tmp." in p.name:
                    continue
                st = p.stat()
            except OSError:
                continue
            out.append(FileEntry(rel, st.st_size, st.st_mtime))
        out.sort()
        return out

    def stat(self, doc_id: str, path: str) -> FileEntry | None:
        try:
            st = self.doc_path(doc_id, path).stat()
        except OSError:
            return None
        return FileEntry(path, st.st_size, st.st_mtime)


store: FileStore = LocalFileStore()


def file_type(path: str) -> str:
    return Path(path).suffix.lower()


def safe_name(name: str, *, new: bool = False) -> str:
    # Relative "a/b/c.png" paths (new) and flat names (old callers, MCP)
    # share this check: segments are sanitized like before, ".." and
    # absolute paths are rejected. Attachments may have any extension.
    raw = (name or "").strip().replace("\\", "/")
    segs = raw.split("/")
    if any(s == ".." for s in segs):
        raise HTTPException(400, "Unsupported file type or path")
    parts = []
    for s in segs:
        if s in ("", "."):
            continue
        n = re.sub(r"[^A-Za-z0-9._ ()-]", "_", s.strip().lstrip("."))[:100]
        if not n:
            raise HTTPException(400, "Unsupported file type or path")
        parts.append(n)
    if not parts:
        raise HTTPException(400, "Unsupported file type or path")
    n = "/".join(parts)
    if ".tmp." in n:
        raise HTTPException(400, "Reserved name")
    if new:
        check_depth(n)
    return n


def check_depth(path: str) -> str:
    """New names may nest at most MAX_FOLDER_DEPTH folders ("a/b/file.typ").
    Older deeper paths (ZIP imports) stay readable; only new writes are held to it."""
    if path.strip("/").count("/") > MAX_FOLDER_DEPTH:
        raise HTTPException(400, f"Folders can be nested at most {MAX_FOLDER_DEPTH} levels deep")
    return path


def safe_rename_target(raw: str, old: str) -> str:
    """Validate a rename target without trapping legacy-deep entries.

    A rename may keep an existing legacy depth, but never add a level beyond
    MAX_FOLDER_DEPTH relative to the source path. New paths still pass the
    normal sanitizer; only the depth check is relaxed for legacy sources.
    """
    target = safe_name(raw)
    depth = target.strip("/").count("/")
    if depth > max(MAX_FOLDER_DEPTH, old.strip("/").count("/")):
        raise HTTPException(400, f"Folders can be nested at most {MAX_FOLDER_DEPTH} levels deep")
    return target
