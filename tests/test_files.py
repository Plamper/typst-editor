from conftest import login, make_doc, project_of, put_file, register_user

from backend import deps
from backend.services import docfiles, project_files


def test_upload_list_get_delete(c):
    register_user(c, "alice")
    pid = project_of(c, make_doc(c, "Dateien"))
    v = put_file(c, pid, "bild.png").json()["version"]
    names = [f["path"] for f in c.get(f"/api/projects/{pid}/files").json()["files"]]
    assert "bild.png" in names
    assert c.get(f"/api/projects/{pid}/files/bild.png").status_code == 200
    assert c.delete(f"/api/projects/{pid}/files/bild.png", params={"version": v}).status_code == 200
    assert c.get(f"/api/projects/{pid}/files/bild.png").status_code == 404


def test_upload_rejects_traversal_and_accepts_generic_files(c):
    register_user(c, "alice")
    pid = project_of(c, make_doc(c, "Dateien2"))
    assert put_file(c, pid, "a/../../evil.sh").status_code in (400, 404)
    assert put_file(c, pid, "tool.exe", b"x").status_code == 200
    response = c.get(f"/api/projects/{pid}/files/tool.exe")
    assert response.content == b"x"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"


def test_upload_over_10mb_rejected(c):
    register_user(c, "alice")
    pid = project_of(c, make_doc(c, "Dateien3"))
    assert put_file(c, pid, "gross.png", b"x" * (10 * 1024 * 1024 + 1)).status_code == 413


def test_svg_file_is_a_download(c):
    register_user(c, "alice")
    pid = project_of(c, make_doc(c, "Dateien4"))
    assert put_file(c, pid, "grafik.svg", b"<svg></svg>").status_code == 200
    r = c.get(f"/api/projects/{pid}/files/grafik.svg")
    assert r.status_code == 200
    assert "attachment" in r.headers.get("content-disposition", "")
    assert r.headers["content-type"] == "application/octet-stream"


def test_reviewer_upload_403(c):
    register_user(c, "alice")
    register_user(c, "bob")
    login(c, "alice")
    did = make_doc(c, "Dateien5")
    pid = project_of(c, did)
    c.post(f"/api/projects/{pid}/members", json={"username": "bob", "role": "reviewer"})
    login(c, "bob")
    assert put_file(c, pid, "x.png").status_code == 403
    assert c.get(f"/api/projects/{pid}/files").json()["readonly"] is True


def test_text_file_limit(c):
    register_user(c, "alice")
    pid = project_of(c, make_doc(c, "Dateien6"))
    assert put_file(c, pid, "notizen.typ", b"hallo").status_code == 200
    assert c.get(f"/api/projects/{pid}/files/notizen.typ").content == b"hallo"
    assert put_file(c, pid, "big.typ", b"x" * 200_001).status_code == 413


def test_write_leaves_no_tmp(c):
    register_user(c, "alice")
    pid = project_of(c, make_doc(c, "DateienTmp"))
    assert put_file(c, pid, "bild.png", b"abc").status_code == 200
    assert put_file(c, pid, "notizen.typ", b"hallo").status_code == 200
    d = deps.get_files_dir() / pid
    assert list(d.glob("*.tmp.*")) == []
    names = [f["path"] for f in c.get(f"/api/projects/{pid}/files").json()["files"]]
    assert names == ["bild.png", "notizen.typ"]


def test_failed_disk_write_leaves_no_row(c, monkeypatch):
    register_user(c, "alice")
    pid = project_of(c, make_doc(c, "Rollback"))
    v = put_file(c, pid, "data.csv", b"first").json()["version"]

    def broken(*args):
        raise OSError("disk full")

    with monkeypatch.context() as m:
        m.setattr(docfiles.store, "put", broken)
        for path, params in (("data.csv", {"version": v}), ("new.csv", {})):
            try:
                put_file(c, pid, path, b"second", **params)
            except OSError:
                pass
    files = c.get(f"/api/projects/{pid}/files").json()["files"]
    assert [(f["path"], f["version"]) for f in files] == [("data.csv", project_files.version(b"first"))]
    assert c.get(f"/api/projects/{pid}/files/data.csv").content == b"first"
