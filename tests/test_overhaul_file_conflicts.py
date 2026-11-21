"""Attachment editors must not silently overwrite a newer shared edit."""
from conftest import login, make_doc, project_of, put_file, register_user


def test_shared_file_stale_version_preserves_newer_content(c):
    register_user(c, "alice")
    pid = project_of(c, make_doc(c))
    version = put_file(c, pid, "chapter.typ", b"Original").json()["version"]
    register_user(c, "bob")
    login(c, "alice")
    assert c.post(f"/api/projects/{pid}/members", json={"username": "bob", "role": "editor"}).status_code == 200
    login(c, "bob")
    assert put_file(c, pid, "chapter.typ", b"Bob edit", version=version).status_code == 200
    login(c, "alice")
    stale = put_file(c, pid, "chapter.typ", b"Alice stale edit", version=version)
    assert stale.status_code == 409
    assert c.get(f"/api/projects/{pid}/files/chapter.typ").content == b"Bob edit"
    fresh = c.get(f"/api/projects/{pid}/files").json()["files"][0]["version"]
    response = put_file(c, pid, "chapter.typ", b"Alice resolved edit", version=fresh)
    assert response.status_code == 200
    assert response.json()["version"] != fresh


def test_version_guard_detects_deleted_file(c):
    register_user(c, "alice")
    pid = project_of(c, make_doc(c))
    version = put_file(c, pid, "chapter.typ", b"Original").json()["version"]
    assert c.delete(f"/api/projects/{pid}/files/chapter.typ", params={"version": version}).status_code == 200
    assert put_file(c, pid, "chapter.typ", b"Zombie edit", version=version).status_code == 409
