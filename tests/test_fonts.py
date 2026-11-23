"""Server-wide and project font discovery, OpenType parsing, API, CLI parity, and guest visibility."""
from __future__ import annotations

import shutil
import struct
from pathlib import Path

import pytest
from conftest import login, put_file, register_user

from backend import config, render
from backend.services import fonts as fonts_svc

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor"


def _vendor_font(prefix: str) -> Path:
    matches = sorted(VENDOR.glob(f"font-{prefix}*"))
    assert matches, f"missing vendored font {prefix}"
    return matches[0]


def _make_minimal_sfnt(family: str, *, typo_family: str = "") -> bytes:
    """Build a minimal valid SFNT (TrueType) binary containing a `name` table."""
    records: list[tuple[int, int, int, int, bytes]] = []
    records.append((3, 1, 0x0409, 1, family.encode("utf-16-be")))
    if typo_family:
        records.append((3, 1, 0x0409, 16, typo_family.encode("utf-16-be")))
    str_offset = 6 + len(records) * 12
    rec_bytes = bytearray()
    str_bytes = bytearray()
    for pid, eid, lid, nid, raw in records:
        rec_bytes += struct.pack(">HHHHHH", pid, eid, lid, nid, len(raw), len(str_bytes))
        str_bytes += raw
    name_table = struct.pack(">HHH", 0, len(records), str_offset) + bytes(rec_bytes) + bytes(str_bytes)
    table_offset = 12 + 16
    offset_table = struct.pack(">4sHHHH", b"\x00\x01\x00\x00", 1, 16, 0, 0)
    dir_entry = struct.pack(">4sIII", b"name", 0, table_offset, len(name_table))
    return offset_table + dir_entry + name_table


def _make_minimal_ttc(families: list[str]) -> bytes:
    """Build a minimal TrueType Collection (.ttc) containing multiple faces."""
    header_size = 12 + 4 * len(families)
    offsets: list[int] = []
    face_blobs: list[bytes] = []
    cur = header_size
    for fam in families:
        # Each face's table offset in a TTC is absolute from start of file.
        records = [(3, 1, 0x0409, 1, fam.encode("utf-16-be"))]
        str_offset = 6 + 12
        raw = records[0][4]
        name_table = struct.pack(">HHH", 0, 1, str_offset) + struct.pack(">HHHHHH", 3, 1, 0x0409, 1, len(raw), 0) + raw
        table_offset = cur + 12 + 16
        face = (
            struct.pack(">4sHHHH", b"\x00\x01\x00\x00", 1, 16, 0, 0)
            + struct.pack(">4sIII", b"name", 0, table_offset, len(name_table))
            + name_table
        )
        offsets.append(cur)
        face_blobs.append(face)
        cur += len(face)
    ttc_head = struct.pack(">4sII", b"ttcf", 0x00010000, len(families)) + struct.pack(f">{len(families)}I", *offsets)
    return ttc_head + b"".join(face_blobs)


def test_opentype_family_extraction_real_and_synthetic():
    assert fonts_svc.extract_font_families_file(_vendor_font("LibertinusSerif-Regular")) == ["Libertinus Serif"]
    assert fonts_svc.extract_font_families_file(_vendor_font("DejaVuSansMono-b4")) == ["DejaVu Sans Mono"]
    assert fonts_svc.extract_font_families_bytes(_make_minimal_sfnt("My Family")) == ["My Family"]
    # Typographic family (Name ID 16) takes precedence over Name ID 1.
    assert fonts_svc.extract_font_families_bytes(_make_minimal_sfnt("My Family Condensed", typo_family="My Family")) == ["My Family"]
    # TTC collections extract all unique faces.
    assert fonts_svc.extract_font_families_bytes(_make_minimal_ttc(["Alpha Sans", "Beta Serif", "Alpha Sans"])) == [
        "Alpha Sans",
        "Beta Serif",
    ]
    # Corrupt / truncated / non-font payloads return [] safely.
    assert fonts_svc.extract_font_families_bytes(b"") == []
    assert fonts_svc.extract_font_families_bytes(b"not a font file at all") == []
    assert fonts_svc.extract_font_families_bytes(b"OTTO\x00\x01") == []


def test_server_fonts_api_and_cache_invalidation(c, tmp_path, monkeypatch):
    font_dir = tmp_path / "srv-fonts"
    font_dir.mkdir()
    shutil.copy(_vendor_font("LibertinusSerif-Regular"), font_dir / "LibertinusSerif-Regular.otf")
    (font_dir / "Fake.ttf").write_bytes(b"corrupt")
    (font_dir / "ignored.woff2").write_bytes(b"wOF2")

    monkeypatch.setenv("FONTS_DIR", str(font_dir))
    fonts_svc.clear_cache()

    # Unauthenticated requests are rejected.
    assert c.get("/api/fonts").status_code == 401
    assert c.get("/api/fonts/0123456789abcdef").status_code == 401

    register_user(c, "alice")
    r = c.get("/api/fonts")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["families"] == ["Libertinus Serif"]
    assert len(body["fonts"]) == 1
    entry = body["fonts"][0]
    assert entry["filename"] == "LibertinusSerif-Regular.otf"
    assert entry["url"] == f"/api/fonts/{entry['id']}"

    # Binary fetch uses immutable private cache and nosniff.
    fb = c.get(entry["url"])
    assert fb.status_code == 200
    assert fb.content == _vendor_font("LibertinusSerif-Regular").read_bytes()
    assert "immutable" in fb.headers.get("cache-control", "")
    assert fb.headers.get("x-content-type-options") == "nosniff"

    # Unknown / malformed IDs return 404.
    assert c.get("/api/fonts/0000000000000000").status_code == 404
    assert c.get("/api/fonts/..%2F..%2Fetc%2Fpasswd").status_code == 404

    # Adding a font invalidates the render content hash.
    h1 = render._content_hash("d1", "hi", [])
    (font_dir / "Custom.ttf").write_bytes(_make_minimal_sfnt("Acme Sans"))
    fonts_svc.clear_cache()
    h2 = render._content_hash("d1", "hi", [])
    assert h1 != h2
    assert "Acme Sans" in c.get("/api/fonts").json()["families"]


def test_default_server_font_dirs_and_override(tmp_path, monkeypatch):
    d1 = tmp_path / "f1"
    d2 = tmp_path / "f2"
    d1.mkdir()
    d2.mkdir()
    monkeypatch.setenv("FONTS_DIR", f"{d1}:{d2}")
    dirs = fonts_svc.server_font_dirs(config.load())
    assert dirs == [d1.resolve(), d2.resolve()]

    # Empty FONTS_DIR includes DATA_DIR/fonts (when present).
    monkeypatch.setenv("FONTS_DIR", "")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    data_fonts = tmp_path / "fonts"
    data_fonts.mkdir()
    dirs_default = fonts_svc.server_font_dirs(config.load())
    assert data_fonts.resolve() in dirs_default


def test_guest_sees_project_fonts_without_quoted_path(c):
    register_user(c, "alice")
    did = c.post("/api/docs/create", json={"title": "Doc", "content": '#set text(font: "Acme Sans")\nHello'}).json()["id"]
    pid = c.get(f"/api/docs/{did}").json()["project_id"]
    font_bytes = _make_minimal_sfnt("Acme Sans")
    assert put_file(c, pid, "fonts/AcmeSans.ttf", font_bytes).status_code == 200
    assert put_file(c, pid, "secret.txt", b"private").status_code == 200

    register_user(c, "bob")
    login(c, "alice")
    assert c.post(f"/api/docs/{did}/share", json={"username": "bob", "role": "reviewer"}).status_code == 200

    login(c, "bob")
    visible_paths = {f["path"] for f in c.get(f"/api/projects/{pid}/files").json()["files"]}
    assert "fonts/AcmeSans.ttf" in visible_paths
    assert "secret.txt" not in visible_paths
    res_paths = {f["path"] for f in c.get(f"/api/docs/{did}/resources").json()["files"]}
    assert "fonts/AcmeSans.ttf" in res_paths
    assert c.get(f"/api/projects/{pid}/files/fonts/AcmeSans.ttf").content == font_bytes


@pytest.mark.skipif(not shutil.which("typst"), reason="typst CLI missing")
def test_typst_cli_uses_project_and_server_fonts(c, tmp_path, monkeypatch):
    # Pick a real system font outside Typst's embedded defaults if available, else fall back to vendored DejaVu.
    extra_dir = tmp_path / "extra-fonts"
    extra_dir.mkdir()
    monkeypatch.setenv("FONTS_DIR", str(extra_dir))
    fonts_svc.clear_cache()

    args = render._font_args()
    assert "--ignore-system-fonts" in args
    assert args.count("--font-path") >= 2
    assert "." in args
    assert str(extra_dir.resolve()) in args

    # Find a non-default real font on disk if present; otherwise verify project font compiles cleanly.
    font_file = _vendor_font("DejaVuSansMono-b4")
    fam = fonts_svc.extract_font_families_file(font_file)[0]
    res = render.check_source(f'#set text(font: "{fam}")\nHello', [("fonts/custom.ttf", font_file.read_bytes())])
    assert res["pages"] == 1
    assert not any("unknown font family" in (w.get("message") or "").lower() for w in res["warnings"])
