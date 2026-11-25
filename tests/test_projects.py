"""Project inheritance, template snapshots into the project tree, isolation and quota."""
from conftest import login, put_file, register_user

from backend import db
from backend.services import project_files


def _paths(c, pid):
    return [f["path"] for f in c.get(f"/api/projects/{pid}/files").json()["files"]]


def setup_project(c):
    register_user(c, "alice")
    c.post("/api/templates", json={"name": "layout.typ", "content": "Owner layout"})
    c.post("/api/templates", json={"name": "private.typ", "content": "SECRET"})
    r = c.post("/api/docs/create", json={"title": "Team", "content": '#include "layout.typ"'})
    assert r.status_code == 200, r.text
    did = r.json()["id"]
    pid = c.get(f"/api/docs/{did}").json()["project_id"]
    assert _paths(c, pid) == ["layout.typ"]  # the used template is snapshotted as a project file
    return pid, did


def test_project_inherits_new_documents_and_revokes(c):
    pid, did = setup_project(c)
    register_user(c, "bob")
    login(c, "alice")
    assert c.post(f"/api/projects/{pid}/members", json={"username": "bob", "role": "editor"}).status_code == 200
    login(c, "bob")
    assert db.doc_role("bob", did) == "editor"
    assert "bob" in c.get(f"/api/docs/{did}/members").json()["members"]
    assert did in [d["id"] for d in c.get("/api/docs").json()["shared"]]
    new = c.post(f"/api/projects/{pid}/docs", json={"title": "New", "folder": "chapters/sub"})
    assert new.status_code == 200, new.text
    nid = new.json()["id"]
    assert db.doc_role("bob", nid) == "editor"
    assert put_file(c, pid, "nested/new.typ", b"Shared").status_code == 200
    login(c, "alice")
    assert c.post(f"/api/projects/{pid}/members", json={"username": "bob", "role": "reviewer"}).status_code == 200
    login(c, "bob")
    assert c.post(f"/api/projects/{pid}/docs", json={"title": "Forbidden"}).status_code == 403
    assert put_file(c, pid, "bad.typ", b"No").status_code == 403
    login(c, "alice")
    assert c.delete(f"/api/projects/{pid}/members/bob").status_code == 200
    login(c, "bob")
    assert c.get(f"/api/docs/{did}").status_code == 404
    assert c.get(f"/api/projects/{pid}/files").status_code == 404


def test_project_snapshot_is_independent_of_the_private_template(c):
    pid, did = setup_project(c)
    c.post("/api/templates", json={"name": "layout.typ", "content": "Changed private"})
    out = c.get(f"/api/docs/{did}/resources").json()
    assert out["project_id"] == pid and out["entry"] == "__doc__.typ"
    assert [f["path"] for f in out["files"]] == ["layout.typ"]
    assert c.get(f"/api/projects/{pid}/files/layout.typ").content == b"Owner layout"
    assert c.get("/api/templates/private.typ").status_code == 200
    assert "private.typ" not in _paths(c, pid)


def test_template_snapshot_lands_where_a_folder_document_reaches_it(c):
    pid, _ = setup_project(c)
    r = c.post(f"/api/projects/{pid}/docs", json={"title": "Sub", "folder": "Ch", "content": '#include "layout.typ"\n#include "/private.typ"'})
    assert r.status_code == 200, r.text
    assert sorted(_paths(c, pid)) == ["Ch/layout.typ", "layout.typ", "private.typ"]


def test_project_editor_document_creation_invalidates_owner_quota(c):
    from backend import sync

    pid, _ = setup_project(c)
    register_user(c, "bob")
    login(c, "alice")
    assert c.post(f"/api/projects/{pid}/members", json={"username": "bob", "role": "editor"}).status_code == 200
    login(c, "bob")
    sync._quota_cache["alice"] = (False, 0)
    sync._quota_cache["bob"] = (False, 0)
    try:
        response = c.post(f"/api/projects/{pid}/docs", json={"title": "Editor created", "content": "New bytes"})
        assert response.status_code == 200, response.text
        assert "alice" not in sync._quota_cache
        assert "bob" in sync._quota_cache
    finally:
        sync._quota_cache.clear()


def test_automatic_project_creation_keeps_individual_shares_limited(c):
    register_user(c, "alice")
    did = c.post("/api/docs/create", json={"title": "Doc"}).json()["id"]
    pid = c.get(f"/api/docs/{did}").json()["project_id"]
    other = c.post(f"/api/projects/{pid}/docs", json={"title": "Other"}).json()["id"]
    register_user(c, "bob")
    login(c, "alice")
    c.post(f"/api/docs/{did}/share", json={"username": "bob", "role": "editor"})
    login(c, "bob")
    assert c.get(f"/api/docs/{did}").status_code == 200
    assert c.get(f"/api/docs/{other}").status_code == 404
    project = c.get(f"/api/projects/{pid}").json()
    assert project["role"] == "guest"
    assert [d["id"] for d in project["documents"]] == [did]


def test_project_paths_resolve_relative_dependencies(c):
    pid, did = setup_project(c)
    for name, content in [("sub/a.typ", b'#include "leaf.typ"'), ("sub/leaf.typ", b"Nested"), ("leaf.typ", b"Root")]:
        assert put_file(c, pid, name, content).status_code == 200
    c.post(f"/api/docs/{did}/save", json={"content": '#include "sub/a.typ"', "force": True})
    out = c.get(f"/api/docs/{did}/resources").json()
    assert sorted(f["path"] for f in out["files"]) == ["sub/a.typ", "sub/leaf.typ"]


def test_project_export_and_duplicate_use_the_project_tree(c):
    from backend.render import _collect_export_source

    pid, did = setup_project(c)
    c.post("/api/templates", json={"name": "layout.typ", "content": "Private changed"})
    _, files, _ = _collect_export_source(did)
    assert dict(files)["layout.typ"] == b"Owner layout"
    duplicate = c.post(f"/api/docs/{did}/duplicate")
    assert duplicate.status_code == 200, duplicate.text
    new = duplicate.json()["id"]
    assert c.get(f"/api/docs/{new}").json()["project_id"] == pid
    assert [f["path"] for f in c.get(f"/api/docs/{new}/resources").json()["files"]] == ["layout.typ"]


def test_guest_duplicate_takes_the_reached_files_into_an_own_project(c):
    pid, did = setup_project(c)
    put_file(c, pid, "other.png", b"x")
    register_user(c, "bob")
    login(c, "alice")
    c.post(f"/api/docs/{did}/share", json={"username": "bob", "role": "editor"})
    login(c, "bob")
    new = c.post(f"/api/docs/{did}/duplicate").json()["id"]
    target = c.get(f"/api/docs/{new}").json()["project_id"]
    assert target != pid
    assert _paths(c, target) == ["layout.typ"]


def test_project_duplicate_uses_owner_document_limit(c, monkeypatch):
    from dataclasses import replace

    from backend import config

    settings = replace(config.load(), MAX_DOCS_PER_USER=3)
    monkeypatch.setattr(config, "load", lambda: settings)
    pid, did = setup_project(c)
    register_user(c, "bob")
    for title in ("Personal one", "Personal two"):
        response = c.post("/api/docs/create", json={"title": title})
        assert response.status_code == 200, response.text
    personal_id = response.json()["id"]
    assert c.post(f"/api/docs/{personal_id}/duplicate").status_code == 400
    login(c, "alice")
    assert c.post(f"/api/projects/{pid}/members", json={"username": "bob", "role": "editor"}).status_code == 200
    login(c, "bob")
    duplicate = c.post(f"/api/docs/{did}/duplicate")
    assert duplicate.status_code == 200, duplicate.text
    with db.tx() as con:
        copied = con.execute("SELECT owner, project_id FROM docs WHERE id=?", (duplicate.json()["id"],)).fetchone()
    assert (copied["owner"], copied["project_id"]) == ("alice", pid)
    blocked = c.post(f"/api/docs/{did}/duplicate")
    assert blocked.status_code == 400, blocked.text
    assert blocked.json()["detail"] == "Too many docs"


def test_project_trash_restore_and_final_delete(c):
    pid, did = setup_project(c)
    assert c.delete(f"/api/projects/{pid}").status_code == 200
    assert pid not in [p["id"] for p in c.get("/api/projects").json()["projects"]]
    assert c.get(f"/api/docs/{did}").status_code == 410
    assert c.post(f"/api/docs/{did}/restore").status_code == 200
    assert c.get("/api/projects").json()["projects"][0]["id"] == pid
    assert _paths(c, pid) == ["layout.typ"]
    c.delete(f"/api/projects/{pid}")
    assert c.delete(f"/api/docs/{did}").status_code == 200
    assert c.get(f"/api/projects/{pid}").status_code == 404


def test_project_quota_failure_is_atomic(c, monkeypatch):
    from backend.services import quota

    pid, did = setup_project(c)
    before = quota.user_bytes("alice")
    monkeypatch.setattr(quota, "quota_cap", lambda: before)
    assert put_file(c, pid, "over.typ", b"x").status_code == 413
    assert _paths(c, pid) == ["layout.typ"]
    assert c.get(f"/api/projects/{pid}/files/over.typ").status_code == 404


def test_project_file_delete_guards_version(c):
    pid, _ = setup_project(c)
    row = c.get(f"/api/projects/{pid}/files").json()["files"][0]
    url = f"/api/projects/{pid}/files/layout.typ"
    assert c.delete(url, params={"version": "stale"}).status_code == 409
    assert c.delete(url, params={"version": row["version"]}).status_code == 200
    assert _paths(c, pid) == []


def test_project_members_see_every_file_but_document_guests_only_reachable(c):
    pid, did = setup_project(c)
    put_file(c, pid, "unused.typ", b"Project-only")
    register_user(c, "member")
    register_user(c, "guest")
    login(c, "alice")
    c.post(f"/api/projects/{pid}/members", json={"username": "member", "role": "reviewer"})
    c.post(f"/api/docs/{did}/share", json={"username": "guest", "role": "reviewer"})
    login(c, "member")
    assert "unused.typ" in _paths(c, pid)
    login(c, "guest")
    assert _paths(c, pid) == ["layout.typ"]
    project = next(p for p in c.get("/api/projects").json()["projects"] if p["id"] == pid)
    assert [f["path"] for f in project["files"]] == ["layout.typ"]


def test_quota_check_observes_uncommitted_project_bytes(c, monkeypatch):
    import pytest
    from fastapi import HTTPException

    from backend.services import quota

    pid, _ = setup_project(c)
    before = quota.user_bytes("alice")
    monkeypatch.setattr(quota, "quota_cap", lambda: before)
    backups: project_files.Backups = {}
    try:
        with db.tx() as con:
            project_files.put(con, pid, "pending.typ", b"one", backups)
            with pytest.raises(HTTPException) as e:
                quota.check_quota("alice", 0, con)
            assert e.value.status_code == 413
            raise RuntimeError("roll back")
    except RuntimeError:
        project_files.undo(backups)


def test_project_file_returns_normalized_name(c):
    register_user(c, "unicode_module")
    project = c.post("/api/projects", json={"title": "Normalization"}).json()["id"]
    response = put_file(c, project, "A\u0308nderungen.typ", b"Text")
    assert response.status_code == 200, response.text
    assert response.json()["path"] == "\u00c4nderungen.typ"
    assert _paths(c, project) == ["\u00c4nderungen.typ"]
