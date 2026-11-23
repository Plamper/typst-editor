"""Server-wide and project font discovery, validation, and OpenType name extraction.

Stdlib only: reads only SFNT/TTC headers and `name` tables (a few KB per file)
so scanning large `/usr/share/fonts` trees finishes in milliseconds without
loading multi-megabyte glyph tables into memory.
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from backend import config

log = logging.getLogger("typst.fonts")

FONT_EXTS: frozenset[str] = frozenset({".ttf", ".otf", ".ttc", ".otc"})
FONT_MIMES: dict[str, str] = {
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".ttc": "font/collection",
    ".otc": "font/collection",
}
_SFNT_TAGS: frozenset[bytes] = frozenset({b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1"})
_VALID_HEADS: frozenset[bytes] = _SFNT_TAGS | frozenset({b"ttcf"})

MAX_SERVER_FONTS = 2000
MAX_SERVER_FONT_FILE_BYTES = 50 * 1024 * 1024
_MAX_TTC_FACES = 256
_MAX_SFNT_TABLES = 256
_MAX_NAME_TABLE_BYTES = 2 * 1024 * 1024
_SCAN_TTL_S = 5.0


@dataclass(frozen=True)
class ServerFont:
    id: str
    filename: str
    path: Path
    size: int
    families: tuple[str, ...]
    mime: str


def is_font_path(path: str) -> bool:
    return Path(path or "").suffix.lower() in FONT_EXTS


def is_valid_font_header(head: bytes) -> bool:
    return len(head) >= 4 and head[:4] in _VALID_HEADS


def _decode_name_string(raw: bytes, platform_id: int, encoding_id: int) -> str:
    try:
        if platform_id == 0 or (platform_id == 3 and encoding_id in (0, 1, 10)):
            s = raw.decode("utf-16-be", errors="ignore")
        elif platform_id == 1 and encoding_id == 0:
            s = raw.decode("mac_roman", errors="ignore")
        else:
            return ""
    except (LookupError, UnicodeDecodeError):
        return ""
    return s.replace("\x00", "").strip()


def _name_priority(platform_id: int, encoding_id: int, language_id: int) -> int:
    # Lower value wins: prefer Windows Unicode US-English, then Unicode, then Mac English.
    if platform_id == 3 and encoding_id in (1, 10):
        return 0 if language_id == 0x0409 else 2
    if platform_id == 0:
        return 1
    if platform_id == 1 and encoding_id == 0:
        return 3 if language_id == 0 else 4
    if platform_id == 3 and encoding_id == 0:
        return 5
    return 99


def _parse_sfnt_face(stream: BinaryIO, offset: int) -> str:
    try:
        stream.seek(offset)
        head = stream.read(12)
        if len(head) < 12:
            return ""
        sfnt_tag, num_tables = struct.unpack(">4sH", head[:6])
        if sfnt_tag not in _SFNT_TAGS or num_tables == 0 or num_tables > _MAX_SFNT_TABLES:
            return ""
        dir_bytes = stream.read(num_tables * 16)
        if len(dir_bytes) < num_tables * 16:
            return ""
        name_offset = 0
        name_length = 0
        for i in range(num_tables):
            rec = dir_bytes[i * 16 : (i + 1) * 16]
            tag, _checksum, tbl_off, tbl_len = struct.unpack(">4sIII", rec)
            if tag == b"name":
                name_offset = tbl_off
                name_length = tbl_len
                break
        if not name_offset or name_length < 6 or name_length > _MAX_NAME_TABLE_BYTES:
            return ""
        stream.seek(name_offset)
        data = stream.read(name_length)
        if len(data) < 6:
            return ""
        _fmt, count, str_off = struct.unpack(">HHH", data[:6])
        if count == 0 or 6 + count * 12 > len(data) or str_off > len(data):
            return ""
        best_16: tuple[int, str] | None = None
        best_1: tuple[int, str] | None = None
        for i in range(count):
            base = 6 + i * 12
            pid, eid, lid, nid, length, off = struct.unpack(">HHHHHH", data[base : base + 12])
            if nid not in (1, 16) or length == 0:
                continue
            prio = _name_priority(pid, eid, lid)
            if prio >= 99:
                continue
            start = str_off + off
            end = start + length
            if end > len(data):
                continue
            val = _decode_name_string(data[start:end], pid, eid)
            if not val:
                continue
            if nid == 16:
                if best_16 is None or prio < best_16[0]:
                    best_16 = (prio, val)
            elif best_1 is None or prio < best_1[0]:
                best_1 = (prio, val)
        if best_16 is not None:
            return best_16[1]
        if best_1 is not None:
            return best_1[1]
        return ""
    except (OSError, struct.error, ValueError):
        return ""


def extract_font_families_stream(stream: BinaryIO) -> list[str]:
    """Extract unique Typst-compatible family names from an OpenType/TrueType/TTC stream."""
    try:
        stream.seek(0)
        head = stream.read(12)
        if not is_valid_font_header(head):
            return []
        families: list[str] = []
        seen: set[str] = set()
        if head[:4] == b"ttcf":
            if len(head) < 12:
                return []
            num_fonts = struct.unpack(">I", head[8:12])[0]
            if num_fonts == 0 or num_fonts > _MAX_TTC_FACES:
                return []
            offs_raw = stream.read(num_fonts * 4)
            if len(offs_raw) < num_fonts * 4:
                return []
            offsets = struct.unpack(f">{num_fonts}I", offs_raw)
            for off in offsets:
                fam = _parse_sfnt_face(stream, off)
                if fam and fam.casefold() not in seen:
                    seen.add(fam.casefold())
                    families.append(fam)
        else:
            fam = _parse_sfnt_face(stream, 0)
            if fam:
                families.append(fam)
        return families
    except (OSError, struct.error, ValueError):
        return []


def extract_font_families_bytes(data: bytes) -> list[str]:
    return extract_font_families_stream(io.BytesIO(data))


def extract_font_families_file(path: Path) -> list[str]:
    try:
        with path.open("rb") as f:
            return extract_font_families_stream(f)
    except OSError:
        return []


def server_font_dirs(cfg: config.Config | None = None) -> list[Path]:
    """Active server-wide font directories (existing directories, deduplicated by real path).

    When FONTS_DIR is set (optionally os.pathsep-separated), only those directories
    are scanned. Otherwise defaults to DATA_DIR/fonts plus standard Linux font mounts
    (/usr/local/fonts, /usr/local/share/fonts, /usr/share/fonts).
    """
    c = cfg if cfg is not None else config.load()
    raw = (c.FONTS_DIR or "").strip()
    candidates: list[Path] = []
    if raw:
        for part in raw.split(os.pathsep):
            s = part.strip()
            if not s:
                continue
            p = Path(s)
            candidates.append(p if p.is_absolute() else config.ROOT / p)
    else:
        candidates = [
            c.DATA_DIR / "fonts",
            Path("/usr/local/fonts"),
            Path("/usr/local/share/fonts"),
            Path("/usr/share/fonts"),
        ]
    out: list[Path] = []
    seen: set[str] = set()
    for p in candidates:
        try:
            if not p.is_dir():
                continue
            real = str(p.resolve())
        except OSError:
            continue
        if real not in seen:
            seen.add(real)
            out.append(Path(real))
    return out


_LOCK = threading.Lock()
# Per-file metadata cache: (real_path, mtime_ns, size) -> (font_id, families)
_FILE_META: dict[tuple[str, int, int], tuple[str, tuple[str, ...]]] = {}
# Scan cache: (dirs_key, scanned_at, entries, by_id, families, fp)
_SCAN_CACHE: tuple[tuple[str, ...], float, tuple[ServerFont, ...], dict[str, ServerFont], tuple[str, ...], str] | None = None


def clear_cache() -> None:
    global _SCAN_CACHE
    with _LOCK:
        _SCAN_CACHE = None
        _FILE_META.clear()


def _font_id_for(real_path: str, mtime_ns: int, size: int) -> str:
    h = hashlib.sha256(f"{real_path}\0{mtime_ns}\0{size}".encode()).hexdigest()
    return h[:16]


def _scan(cfg: config.Config | None = None) -> tuple[tuple[ServerFont, ...], dict[str, ServerFont], tuple[str, ...], str]:
    global _SCAN_CACHE
    dirs = server_font_dirs(cfg)
    dirs_key = tuple(str(d) for d in dirs)
    now = time.monotonic()
    with _LOCK:
        if _SCAN_CACHE is not None and _SCAN_CACHE[0] == dirs_key and (now - _SCAN_CACHE[1]) < _SCAN_TTL_S:
            return _SCAN_CACHE[2], _SCAN_CACHE[3], _SCAN_CACHE[4], _SCAN_CACHE[5]

    entries: list[ServerFont] = []
    by_id: dict[str, ServerFont] = {}
    seen_real: set[str] = set()
    fam_by_cf: dict[str, str] = {}

    for root in dirs:
        if len(entries) >= MAX_SERVER_FONTS:
            break
        try:
            walker = os.walk(root, followlinks=True)
        except OSError:
            continue
        visited_dirs: set[tuple[int, int]] = set()
        for dirpath, dirnames, filenames in walker:
            if len(entries) >= MAX_SERVER_FONTS:
                break
            try:
                dst = os.stat(dirpath)
                dkey = (dst.st_dev, dst.st_ino)
                if dkey in visited_dirs:
                    dirnames[:] = []
                    continue
                visited_dirs.add(dkey)
            except OSError:
                dirnames[:] = []
                continue
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                if len(entries) >= MAX_SERVER_FONTS:
                    break
                if name.startswith("."):
                    continue
                ext = Path(name).suffix.lower()
                if ext not in FONT_EXTS:
                    continue
                p = Path(dirpath) / name
                try:
                    real = str(p.resolve())
                    if real in seen_real:
                        continue
                    st = os.stat(real)
                    if not Path(real).is_file() or st.st_size <= 12 or st.st_size > MAX_SERVER_FONT_FILE_BYTES:
                        continue
                except OSError:
                    continue
                seen_real.add(real)
                meta_key = (real, st.st_mtime_ns, st.st_size)
                with _LOCK:
                    cached = _FILE_META.get(meta_key)
                if cached is None:
                    fams = tuple(extract_font_families_file(Path(real)))
                    fid = _font_id_for(real, st.st_mtime_ns, st.st_size)
                    with _LOCK:
                        if len(_FILE_META) > MAX_SERVER_FONTS * 4:
                            _FILE_META.clear()
                        _FILE_META[meta_key] = (fid, fams)
                else:
                    fid, fams = cached
                if not fams:
                    continue
                entry = ServerFont(
                    id=fid,
                    filename=name,
                    path=Path(real),
                    size=st.st_size,
                    families=fams,
                    mime=FONT_MIMES.get(ext, "application/octet-stream"),
                )
                entries.append(entry)
                by_id[fid] = entry
                for fam in fams:
                    fam_by_cf.setdefault(fam.casefold(), fam)

    families = tuple(sorted(fam_by_cf.values(), key=str.casefold))
    if entries:
        h = hashlib.sha256()
        for e in sorted(entries, key=lambda x: x.id):
            h.update(e.id.encode("ascii"))
            h.update(b":")
            h.update(str(e.size).encode("ascii"))
            h.update(b"\0")
        fp = h.hexdigest()[:16]
    else:
        fp = "none"

    tup = tuple(entries)
    with _LOCK:
        _SCAN_CACHE = (dirs_key, now, tup, by_id, families, fp)
    return tup, by_id, families, fp


def list_server_fonts(cfg: config.Config | None = None) -> list[ServerFont]:
    return list(_scan(cfg)[0])


def get_server_font(font_id: str, cfg: config.Config | None = None) -> ServerFont | None:
    return _scan(cfg)[1].get(font_id)


def server_font_families(cfg: config.Config | None = None) -> list[str]:
    return list(_scan(cfg)[2])


def fingerprint(cfg: config.Config | None = None) -> str:
    return _scan(cfg)[3]
