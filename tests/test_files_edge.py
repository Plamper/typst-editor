"""Files edge: avatar happy path, repeated delete, 200KB limit."""
from conftest import make_doc, project_of, put_file, register_user

PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQ"
       "DwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def test_avatar_upload_and_get(c):
    register_user(c, "alice")
    assert c.post("/api/me/avatar", json={"img": PNG}).status_code == 200
    assert c.get("/api/me").json()["hasAvatar"] is True
    r = c.get("/api/avatar/alice")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert len(r.content) > 0


def test_avatar_delete_then_404(c):
    register_user(c, "alice")
    assert c.post("/api/me/avatar", json={"img": PNG}).status_code == 200
    assert c.delete("/api/me/avatar").status_code == 200
    assert c.get("/api/avatar/alice").status_code == 404


def test_delete_file_twice_is_404(c):
    register_user(c, "alice")
    pid = project_of(c, make_doc(c, "Dateien"))
    v = put_file(c, pid, "bild.png").json()["version"]
    assert c.delete(f"/api/projects/{pid}/files/bild.png", params={"version": v}).status_code == 200
    assert c.delete(f"/api/projects/{pid}/files/bild.png", params={"version": v}).status_code == 404
    assert c.get(f"/api/projects/{pid}/files/bild.png").status_code == 404


def test_text_save_boundary(c):
    register_user(c, "alice")
    pid = project_of(c, make_doc(c, "Grenze"))
    v = put_file(c, pid, "kurz.typ", b"x" * 199999).json()["version"]
    assert put_file(c, pid, "kurz.typ", b"x" * 200000, version=v).status_code == 200
    assert put_file(c, pid, "kurz.typ", b"x" * 200001, force=1).status_code == 413
