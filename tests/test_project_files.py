"""One file tree per project: routes, folder-relative compile, convert, ZIP import."""
import io
import json
import shutil
import zipfile

import pytest
from conftest import login, register_user

from backend import db, render
from backend.services import project_files


def _project(c, title="Doc", content="hi"):
    did = c.post("/api/docs/create", json={"title": title, "content": content}).json()["id"]
    return c.get(f"/api/docs/{did}").json()["project_id"], did


def _put(c, pid, path, data, **params):
    return c.put(f"/api/projects/{pid}/files/{path}", content=data, params=params)


def test_file_routes_versions_and_trash(c):
    register_user(c, "alice")
    pid, _ = _project(c)
    r = _put(c, pid, "img/logo.png", b"\x89PNG")
    assert r.status_code == 200, r.text
    v = r.json()["version"]
    assert v == project_files.version(b"\x89PNG")
    listed = c.get(f"/api/projects/{pid}/files").json()
    assert listed == {"files": [{"path": "img/logo.png", "size": 4, "version": v, "updated_at": listed["files"][0]["updated_at"]}], "readonly": False}
    got = c.get(f"/api/projects/{pid}/files/img/logo.png")
    assert got.content == b"\x89PNG"
    assert got.headers["content-disposition"] == "attachment; filename*=UTF-8''logo.png"
    assert got.headers["x-content-type-options"] == "nosniff"
    assert _put(c, pid, "img/logo.png", b"new").status_code == 409  # overwrite needs the version
    assert _put(c, pid, "IMG/LOGO.png", b"x").status_code == 409  # case-insensitive unique
    assert _put(c, pid, "a/__doc__.typ", b"x").status_code == 400  # reserved
    v2 = _put(c, pid, "img/logo.png", b"new", version=v).json()["version"]
    assert _put(c, pid, "img/logo.png", b"forced", force=1).status_code == 200
    assert c.delete(f"/api/projects/{pid}/files/img/logo.png", params={"version": v2}).status_code == 409
    v3 = project_files.version(b"forced")
    assert c.delete(f"/api/projects/{pid}/files/img/logo.png", params={"version": v3}).status_code == 200
    assert c.get(f"/api/projects/{pid}/files").json()["files"] == []
    entry = next(t for t in c.get("/api/docs").json()["trash"] if t.get("kind") == "file")
    assert entry["title"] == "img/logo.png" and entry["project_id"] == pid and entry["doc_id"] == ""
    assert c.post(f"/api/trash/{entry['id']}/restore").status_code == 200
    assert c.get(f"/api/projects/{pid}/files/img/logo.png").content == b"forced"
    p = next(p for p in c.get("/api/projects").json()["projects"] if p["id"] == pid)
    assert p["files"] == [{"path": "img/logo.png", "size": 6, "version": v3}]
    assert "resources" not in p and all("files" not in d for d in p["documents"])


def test_move_rewrites_references(c):
    register_user(c, "alice")
    pid, did = _project(c, content='#include "Chapters/part.typ"')
    _put(c, pid, "Chapters/part.typ", b'#image("../img/a.png")')
    _put(c, pid, "img/a.png", b"png")
    pre = c.post(f"/api/projects/{pid}/files-move-preview", json={"path": "Chapters/part.typ", "target": "Parts/part.typ"}).json()
    assert pre["references"] == ["Doc: Chapters/part.typ"]
    r = c.post(f"/api/projects/{pid}/files-move", json={"path": "Chapters/part.typ", "target": "Parts/deep/part.typ", "version": pre["version"]})
    assert r.status_code == 200, r.text
    assert r.json() == {"path": "Parts/deep/part.typ", "updated_references": 1}
    assert c.get(f"/api/docs/{did}").json()["content"] == '#include "Parts/deep/part.typ"'
    assert c.get(f"/api/projects/{pid}/files/Parts/deep/part.typ").content == b'#image("../../img/a.png")'
    old = project_files.version(b"png")
    assert c.post(f"/api/projects/{pid}/files-move", json={"path": "img/a.png", "target": "img/b.png", "version": "stale"}).status_code == 409
    assert c.post(f"/api/projects/{pid}/files-move", json={"path": "img/a.png", "target": "img/b.png", "version": old}).status_code == 200
    assert c.get(f"/api/projects/{pid}/files/Parts/deep/part.typ").content == b'#image("../../img/b.png")'


def test_guest_sees_only_what_their_document_reaches(c):
    register_user(c, "alice")
    pid, did = _project(c, content='#include "a.typ"')
    _put(c, pid, "a.typ", b'#image("b.png")')
    _put(c, pid, "b.png", b"b")
    _put(c, pid, "secret.txt", b"s")
    register_user(c, "bob")
    login(c, "alice")
    assert c.post(f"/api/docs/{did}/share", json={"username": "bob", "role": "reviewer"}).status_code == 200
    login(c, "bob")
    assert {f["path"] for f in c.get(f"/api/projects/{pid}/files").json()["files"]} == {"a.typ", "b.png"}
    assert c.get(f"/api/projects/{pid}/files/secret.txt").status_code == 404
    assert c.get(f"/api/projects/{pid}/files/b.png").content == b"b"
    assert _put(c, pid, "x.txt", b"x").status_code == 404


def test_compile_root_is_folder_relative(c):
    register_user(c, "alice")
    pid, did = _project(c, content='#include "part.typ"')
    c.post(f"/api/projects/{pid}/docs", json={"title": "Sub", "folder": "Chapters", "content": '#include "part.typ"\n#read("../data.txt")'})
    sub = next(d["id"] for d in c.get(f"/api/projects/{pid}").json()["documents"] if d["title"] == "Sub")
    _put(c, pid, "Chapters/part.typ", b"part")
    _put(c, pid, "data.txt", b"data")
    main, files, skipped = render._collect_export_source(sub)
    assert files[0] == ("Chapters/__doc__.typ", main.encode())
    assert dict(files)["Chapters/part.typ"] == b"part" and skipped == []
    payload, _mime, name = render.build_export_payload("Sub", main, files, "zip")
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        assert sorted(z.namelist()) == ["Chapters/Sub.typ", "Chapters/part.typ", "data.txt"]
    assert name == "Sub.zip"
    assert render._entry(render._collect_export_source(did)[1]) == "__doc__.typ"


@pytest.mark.skipif(not shutil.which("typst"), reason="typst CLI missing")
def test_typst_compiles_parent_references(c):
    register_user(c, "alice")
    pid, _ = _project(c)
    c.post(f"/api/projects/{pid}/docs", json={"title": "Sub", "folder": "Chapters", "content": '#include "part.typ"\n#read("../data.txt")\n#read("/data.txt")'})
    sub = next(d["id"] for d in c.get(f"/api/projects/{pid}").json()["documents"] if d["title"] == "Sub")
    _put(c, pid, "Chapters/part.typ", b"Part")
    _put(c, pid, "data.txt", b"data")
    r = c.post(f"/api/docs/{sub}/export", params={"format": "pdf"})
    assert r.status_code == 200, r.text
    assert r.content.startswith(b"%PDF")


def test_convert_document_and_file(c):
    register_user(c, "alice")
    pid, did = _project(c, content='#include "Ch/Part.typ"')
    c.post(f"/api/projects/{pid}/docs", json={"title": "Part", "folder": "Ch", "content": "= Part"})
    part = next(d["id"] for d in c.get(f"/api/projects/{pid}").json()["documents"] if d["title"] == "Part")
    r = c.post(f"/api/projects/{pid}/convert", json={"doc_id": part})
    assert r.status_code == 200, r.text
    assert r.json() == {"path": "Ch/Part.typ", "broken": []}
    assert c.get(f"/api/projects/{pid}/files/Ch/Part.typ").content == b"= Part"
    assert c.get(f"/api/docs/{part}").status_code == 410
    r = c.post(f"/api/projects/{pid}/convert", json={"path": "Ch/Part.typ"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["title"] == "Part" and out["broken"] == ["Doc: Ch/Part.typ"]
    doc = c.get(f"/api/docs/{out['id']}").json()
    assert (doc["folder"], doc["content"]) == ("Ch", "= Part")
    assert c.get(f"/api/projects/{pid}/files").json()["files"] == []
    _put(c, pid, "data.csv", b"a,b")
    assert c.post(f"/api/projects/{pid}/convert", json={"path": "data.csv"}).status_code == 400
    assert c.post(f"/api/projects/{pid}/demote", json={"doc_id": did, "target_doc_id": did}).status_code in (404, 405)


def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_zip_import_many_and_no_documents(c):
    register_user(c, "alice")
    data = _zip({"main.typ": '#include "ch/a.typ"', "ch/a.typ": "A", "other.typ": "O", "img/x.png": b"png"})
    pre = c.post("/api/zip-preview", files={"f": ("thesis.zip", data)}).json()
    assert pre == {"entries": ["ch/a.typ", "main.typ", "other.typ"], "suggested": ["main.typ", "other.typ"]}
    r = c.post("/api/docs/import-zip", files={"f": ("thesis.zip", data)}, data={"name": "thesis.zip", "documents": json.dumps(["main.typ", "other.typ"])})
    assert r.status_code == 200, r.text
    out = r.json()
    assert len(out["documents"]) == 2 and out["files"] == 2
    p = c.get(f"/api/projects/{out['project_id']}").json()
    assert p["title"] == "thesis"
    assert sorted((d["folder"], d["title"]) for d in p["documents"]) == [("", "main"), ("", "other")]
    assert sorted(f["path"] for f in p["files"]) == ["ch/a.typ", "img/x.png"]
    r = c.post(f"/api/projects/{out['project_id']}/import-zip", files={"f": ("t.zip", data)}, data={"folder": "copy", "documents": "[]"})
    assert r.status_code == 200, r.text
    assert r.json()["documents"] == [] and r.json()["files"] == 4
    paths = {f["path"] for f in c.get(f"/api/projects/{out['project_id']}/files").json()["files"]}
    assert {"copy/main.typ", "copy/ch/a.typ", "copy/img/x.png"} <= paths
    assert c.post(f"/api/projects/{out['project_id']}/import-zip", files={"f": ("t.zip", data)}, data={"folder": "copy"}).status_code == 409
    assert c.post("/api/docs/import-zip", files={"f": ("t.zip", data)}, data={"name": "t.zip", "documents": '["img/x.png"]'}).status_code == 400
    assert c.post("/api/docs/import-zip", files={"f": ("t.zip", _zip({"../x.typ": "x"}))}, data={"name": "t"}).status_code == 400


def test_folder_rename_and_delete_carry_files(c):
    register_user(c, "alice")
    pid, _ = _project(c)
    c.post(f"/api/projects/{pid}/docs", json={"title": "In", "folder": "A"})
    _put(c, pid, "A/x.txt", b"x")
    assert c.post(f"/api/projects/{pid}/folders/rename", json={"path": "A", "target": "B"}).status_code == 200
    p = c.get(f"/api/projects/{pid}").json()
    assert [f["path"] for f in p["files"]] == ["B/x.txt"] and any(d["folder"] == "B" for d in p["documents"])
    assert c.delete(f"/api/projects/{pid}/folders", params={"path": "B"}).status_code == 200
    p = c.get(f"/api/projects/{pid}").json()
    assert p["files"] == [] and all(d["folder"] != "B" for d in p["documents"])
    with db.tx() as con:
        assert con.execute("SELECT name FROM trash_entries WHERE kind='file' AND project_id=?", (pid,)).fetchone()[0] == "B/x.txt"
