from conftest import login, put_file, register_user


def project(c, title):
    r = c.post("/api/projects", json={"title": title})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def pull(c, body):
    p = c.post("/api/library/pull-preview", json=body)
    assert p.status_code == 200, p.text
    assert p.json()["collisions"] == []
    r = c.post("/api/library/pull", json={**body, "preview": p.json()["preview"]})
    assert r.status_code == 200, r.text
    return r.json()


def tree(c, pid):
    return {f["path"]: c.get(f"/api/projects/{pid}/files/{f['path']}").text for f in c.get(f"/api/projects/{pid}/files").json()["files"]}


def test_personal_module_pull_copies_dependencies_and_is_independent(c):
    register_user(c, "alice")
    pid = project(c, "Team")
    c.post("/api/templates", json={"name": "layout.typ", "content": '#include "leaf.typ"'})
    c.post("/api/templates", json={"name": "leaf.typ", "content": "Snapshot"})
    body = {"source_scope": "personal", "source": "layout.typ", "scope": "project", "project_id": pid, "name": "modules/layout.typ"}
    pull(c, body)
    rows = tree(c, pid)
    assert rows["modules/layout.typ"] == '#include "leaf.typ"'
    assert rows["modules/leaf.typ"] == "Snapshot"
    c.post("/api/templates", json={"name": "leaf.typ", "content": "Changed original"})
    assert tree(c, pid)["modules/leaf.typ"] == "Snapshot"


def test_reader_can_pull_project_files_but_cannot_write_project(c):
    register_user(c, "alice")
    pid = project(c, "Shared")
    put_file(c, pid, "lib/data.csv", b"a,b\n1,2")
    register_user(c, "bob")
    login(c, "alice")
    c.post(f"/api/projects/{pid}/members", json={"username": "bob", "role": "reviewer"})
    login(c, "bob")
    body = {"source_scope": "project", "source_project_id": pid, "source": "lib/data.csv", "scope": "personal", "name": "data.csv"}
    pull(c, body)
    assert c.get("/api/templates/data.csv").json()["content"] == "a,b\n1,2"
    blocked = {**body, "scope": "project", "project_id": pid, "name": "another.csv"}
    assert c.post("/api/library/pull-preview", json=blocked).status_code == 403
    register_user(c, "outsider")
    assert c.post("/api/library/pull-preview", json=body).status_code == 404


def test_pull_conflicts_and_source_changes_do_not_partially_copy(c):
    register_user(c, "alice")
    pid = project(c, "Team")
    c.post("/api/templates", json={"name": "a.typ", "content": '#include "b.typ"'})
    c.post("/api/templates", json={"name": "b.typ", "content": "Old"})
    body = {"source_scope": "personal", "source": "a.typ", "scope": "project", "project_id": pid, "name": "a.typ"}
    p = c.post("/api/library/pull-preview", json=body).json()
    c.post("/api/templates", json={"name": "b.typ", "content": "New"})
    assert c.post("/api/library/pull", json={**body, "preview": p["preview"]}).status_code == 409
    assert tree(c, pid) == {}
    put_file(c, pid, "b.typ", b"Existing")
    p = c.post("/api/library/pull-preview", json=body).json()
    assert p["collisions"] == ["b.typ"]
    assert c.post("/api/library/pull", json={**body, "preview": p["preview"]}).status_code == 409
    assert list(tree(c, pid)) == ["b.typ"]
    pull(c, {**body, "renames": {"b.typ": "b-copy.typ"}})
    rows = tree(c, pid)
    assert rows["a.typ"] == '#include "b-copy.typ"'
    assert rows["b.typ"] == "Existing"


def test_project_to_personal_pull_preserves_nested_reference_meaning(c):
    register_user(c, "alice")
    pid = project(c, "Team")
    for name, content in [("modules/a.typ", b'#include "../data/b.typ"'), ("data/b.typ", b"Leaf")]:
        put_file(c, pid, name, content)
    pull(c, {"source_scope": "project", "source_project_id": pid, "source": "modules/a.typ", "scope": "personal", "name": "a.typ"})
    assert c.get("/api/templates/a.typ").json()["content"] == '#include "b.typ"'
    assert c.get("/api/templates/b.typ").json()["content"] == "Leaf"


def test_document_guest_can_pull_only_files_their_document_reaches(c):
    register_user(c, "alice")
    pid = project(c, "Team")
    did = c.post(f"/api/projects/{pid}/docs", json={"title": "Report", "content": '#include "used.typ"'}).json()["id"]
    for name in ("used.typ", "hidden.typ"):
        put_file(c, pid, name, name.encode())
    register_user(c, "guest")
    login(c, "alice")
    c.post(f"/api/docs/{did}/share", json={"username": "guest", "role": "reviewer"})
    login(c, "guest")
    b = {"source_scope": "project", "source_project_id": pid, "source": "used.typ", "scope": "personal", "name": "mine.typ"}
    pull(c, b)
    assert c.post("/api/library/pull-preview", json={**b, "source": "hidden.typ"}).status_code == 404
    assert c.post(f"/api/docs/{did}/library-copy-preview", json={"source": "used.typ", "scope": "personal", "name": "x.typ"}).status_code == 404
