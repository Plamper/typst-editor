"""Text resources and quoted references.

Personal templates (.typ, .bib, .csv, data) live in the `templates` table,
per account, never public. Project files are reached through quoted paths
(`resolve`, `reachable`): a document in folder F compiles as F/__doc__.typ.
"""
from __future__ import annotations

import posixpath
import re
import sqlite3
import unicodedata
from pathlib import Path

from fastapi import HTTPException

from backend.constants import ENTRY

RESOURCE_EXTS = {".typ": "template", ".bib": "library", ".csv": "table", ".tsv": "table", **dict.fromkeys((".json", ".yaml", ".yml", ".toml", ".xml", ".txt", ".csl"), "data")}
REF_RE = re.compile(r'"([^"\n]{1,240})"')
# Explicit bound (not silent truncation) on the include closure. Typical docs reach a
# handful of files; the cap only stops cycles and pathological inputs. A capped-away
# file surfaces as a normal Typst "file not found" compile error, never as silent success.
REACHABLE_MAX = 500
# The compile entry file is the doc itself: a resource by that name would shadow it in
# every compile. New/updated resources reject it; get/read/delete keep working for any pre-existing rows.
RESERVED_NAMES = frozenset({ENTRY})
# What a path segment cannot hold: file system and quoted-reference delimiters, control, line-separator and bidi-override
# characters, lone surrogates. Every other character, any script and emoji included, is a valid name.
NAME_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069\ud800-\udfff]')
NAME_MAX_BYTES = 200  # a file system caps a name at 255 bytes, the atomic-write tmp suffix needs the rest


def clean_segment(raw: str) -> str:
    n = NAME_BAD.sub("_", unicodedata.normalize("NFC", raw.strip().lstrip(".")))[:100]
    while len(n.encode()) > NAME_MAX_BYTES:
        n = n[:-1]
    return n


def kind_of(name: str) -> str:
    return RESOURCE_EXTS.get(Path(name or "").suffix.lower(), "")


def res_name(raw: str) -> str:
    n = clean_segment(Path(raw or "").name)
    if not n or kind_of(n) == "":
        raise HTTPException(400, "Unsupported text resource extension")
    return n


def is_reserved(name: str) -> bool:
    return (name or "").strip().lower() == ENTRY


def insert_line(name: str) -> str:
    low = (name or "").lower()
    if low.endswith(".bib"):
        return f'#bibliography("{name}")'
    if low.endswith(".typ"):
        return f'#include "{name}"'
    if low.endswith(".csv"):
        return f'#csv("{name}")'
    suffix = Path(low).suffix
    loader = {".tsv": "csv", ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".xml": "xml", ".txt": "read", ".csl": "read"}.get(suffix)
    if loader:
        delimiter = ', delimiter: "\\t"' if suffix == ".tsv" else ""
        return f'#let data = {loader}("{name}"{delimiter})'
    return ""


def resolve(base: str, ref: str) -> str | None:
    """Project path a quoted reference from a text in folder `base` points at: relative to
    that folder, "/x" from the project root, None when it would leave the root (Typst refuses)."""
    raw = (ref or "").strip().replace("\\", "/")
    if not raw:
        return None
    p = posixpath.normpath(raw.lstrip("/") if raw.startswith("/") else posixpath.join(base or "", raw))
    if p in ("", ".", "..") or p.startswith("../"):
        return None
    return p


def reachable(paths: set[str], typ: dict[str, str], folder: str, text: str) -> set[str]:
    """Project files a document in `folder` reaches, transitively over the .typ files it includes."""
    got: set[str] = set()
    queue = [(folder, text or "")]
    while queue and len(got) < REACHABLE_MAX:
        base, body = queue.pop()
        for m in REF_RE.finditer(body):
            p = resolve(base, m.group(1))
            if p in paths and p not in got:
                got.add(p)
                if p in typ:
                    queue.append((posixpath.dirname(p), typ[p]))
    return got


def list_resources(con: sqlite3.Connection, owner: str) -> list[dict]:
    rows = con.execute("SELECT name, content, line, folder, updated_at FROM templates "
                       "WHERE owner=? ORDER BY name", (owner,)).fetchall()
    return [{"name": r["name"], "content": r["content"] or "", "line": r["line"] or "",
             "folder": r["folder"] or "", "kind": kind_of(r["name"]),
             "updated_at": r["updated_at"] or ""} for r in rows]


def copy_name(name: str, number: int = 1, lang: str = "en") -> str:
    suffix = Path(name).suffix
    stem = name[:-len(suffix)] if suffix else name
    tag = (' (Kopie)' if lang == 'de' else ' (copy)') if number == 1 else (f' (Kopie {number})' if lang == 'de' else f' (copy {number})')
    return stem[:100 - len(tag) - len(suffix)] + tag + suffix
