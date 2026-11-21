"""Overhaul resources: account-global .typ/.bib/.csv shelf, project snapshots, reachability, exports."""
import io
import zipfile

from conftest import login, make_doc, project_of, put_file, register_user


def _res(c, name, content="#x", folder="", version=""):
    body = {"name": name, "content": content, "folder": folder}
    if version:
        body["version"] = version
    return c.post("/api/templates", json=body)


def _snapshot(c, did, name):
    pid = c.get(f"/api/docs/{did}").json()["project_id"]
    body = {"source_scope": "personal", "source": name, "scope": "project", "project_id": pid, "name": name}
    p = c.post("/api/library/pull-preview", json=body)
    assert p.status_code == 200, p.text
    r = c.post("/api/library/pull", json={**body, "preview": p.json()["preview"]})
    assert r.status_code == 200, r.text
    return r.json()


def test_create_list_kinds_and_backward_compat(c):
    register_user(c, "u1")
    assert _res(c, "a.typ", "#include me").status_code == 200
    assert _res(c, "refs.bib", "@book{k1,}").status_code == 200
    assert _res(c, "data.csv", "a,b\n1,2").status_code == 200
    assert _res(c, "evil.exe", "x").status_code == 400
    assert _res(c, "noext", "x").status_code == 400
    r = c.get("/api/templates")
    assert r.status_code == 200
    rows = {t["name"]: t for t in r.json()["templates"]}
    assert rows["a.typ"]["kind"] == "template"
    assert rows["refs.bib"]["kind"] == "library"
    assert rows["data.csv"]["kind"] == "table"
    assert all(t["updated_at"] for t in rows.values())  # version marker present
    assert rows["refs.bib"]["line"].startswith("#bibliography")


def test_two_users_same_filename_isolated(c):
    register_user(c, "alice")
    _res(c, "lib.typ", "ALICE")
    login(c, "alice")  # stay alice; now create bob
    register_user(c, "bob")
    login(c, "bob")
    assert _res(c, "lib.typ", "BOB").status_code == 200
    r = c.get("/api/templates")
    assert [t for t in r.json()["templates"] if t["name"] == "lib.typ"][0]["content"] == "BOB"
    # bob cannot touch alice's shelf: delete hits only his own row
    assert c.delete("/api/templates/lib.typ").status_code == 200
    login(c, "alice")
    r = c.get("/api/templates")
    assert [t for t in r.json()["templates"] if t["name"] == "lib.typ"][0]["content"] == "ALICE"


def test_shared_doc_reaches_snapshotted_files_transitively(c):
    register_user(c, "own")
    did = make_doc(c, title="Shared", content='#include "mid.typ"')
    _res(c, "mid.typ", '#include "leaf.typ"\n#bibliography("refs.bib")')
    _res(c, "leaf.typ", "LEAF")
    _res(c, "refs.bib", "@book{k1,}")
    _res(c, "unrelated.typ", "NOPE")  # must NOT leak into the project
    _snapshot(c, did, "mid.typ")
    register_user(c, "viw")
    login(c, "own")
    assert c.post(f"/api/docs/{did}/share", json={"username": "viw", "role": "editor"}).status_code == 200
    login(c, "viw")
    r = c.get(f"/api/docs/{did}/resources")
    assert r.status_code == 200
    body = r.json()
    assert body["readonly"] is True
    assert {f["path"] for f in body["files"]} == {"mid.typ", "leaf.typ", "refs.bib"}


def test_existing_project_file_wins_over_template(c):
    register_user(c, "own")
    pid = project_of(c, make_doc(c, title="D"))
    _res(c, "same.typ", "OWNER VERSION")
    assert put_file(c, pid, "same.typ", b"PROJECT VERSION").status_code == 200
    r = c.post(f"/api/projects/{pid}/docs", json={"title": "E", "content": '#include "same.typ"'})
    assert r.status_code == 200, r.text
    assert c.get(f"/api/projects/{pid}/files/same.typ").content == b"PROJECT VERSION"


def test_denied_mutations_and_unshare_revocation(c):
    register_user(c, "own")
    did = make_doc(c, title="D", content="hi")
    pid = project_of(c, did)
    _res(c, "own.typ", "O")
    register_user(c, "viw")
    login(c, "own")
    c.post(f"/api/docs/{did}/share", json={"username": "viw", "role": "reviewer"})
    login(c, "viw")
    # reviewer cannot write project files...
    assert put_file(c, pid, "x.typ", b"z").status_code in (403, 404)
    # ...but CAN read what the document reaches (none here)
    assert c.get(f"/api/docs/{did}/resources").status_code == 200
    # unshare revokes access too
    login(c, "own")
    assert c.delete(f"/api/docs/{did}/share/viw").status_code == 200
    login(c, "viw")
    assert c.get(f"/api/docs/{did}/resources").status_code == 404


def test_version_conflict(c):
    register_user(c, "usr")
    _res(c, "a.typ", "v1")
    v1 = c.get("/api/templates/a.typ").json()["version"]
    assert _res(c, "a.typ", "v2", version=v1).status_code == 200
    v2 = c.get("/api/templates/a.typ").json()["version"]
    assert v2 != v1
    r = _res(c, "a.typ", "stale-write", version=v1)  # stale guard
    assert r.status_code == 409
    assert c.get("/api/templates/a.typ").json()["content"] == "v2"  # no clobber
    # no version = legacy path still saves
    assert _res(c, "a.typ", "v3").status_code == 200


def test_duplicate_and_folders(c):
    register_user(c, "usr")
    _res(c, "a.bib", "@x", folder="F")
    r = c.post("/api/templates/a.bib/duplicate", json={"name": "b.bib"})
    assert r.status_code == 200, r.text
    assert c.post("/api/templates/a.bib/duplicate", json={"name": "c.typ"}).status_code == 400
    rows = {t["name"]: t for t in c.get("/api/templates").json()["templates"]}
    assert rows["b.bib"]["folder"] == "F"
    assert c.post("/api/templates/b.bib/folder", json={"folder": ""}).status_code == 200
    assert c.get("/api/tplfolders").json()["folders"]  # shared namespace intact
    assert c.post('/api/templates', json={"name": "a.bib", "content": "Updated content"}).status_code == 200
    assert c.get("/api/templates/a.bib").json()["folder"] == "F"  # autosave omitted folder


def test_resource_rename_is_atomic_and_preserves_folder(c):
    register_user(c, "renamer")
    assert _res(c, "old.bib", "@book{key,}", folder="Research").status_code == 200
    version = c.get('/api/templates/old.bib').json()['version']
    assert _res(c, "taken.bib", "@book{other,}").status_code == 200
    assert c.post('/api/templates/old.bib/rename', json={"name": "taken.bib", "version": version}).status_code == 409
    assert c.get('/api/templates/old.bib').json()['content'] == '@book{key,}'
    renamed = c.post('/api/templates/old.bib/rename', json={"name": "new.bib", "version": version})
    assert renamed.status_code == 200, renamed.text
    assert c.get('/api/templates/old.bib').status_code == 404
    row = c.get('/api/templates/new.bib').json()
    assert row['content'] == '@book{key,}' and row['folder'] == 'Research'
    assert c.post('/api/templates/new.bib/rename', json={"name": "new.typ"}).status_code == 400


def test_resource_rename_does_not_erase_a_concurrent_edit(c):
    register_user(c, "renamer")
    assert _res(c, "old.typ", "Before").status_code == 200
    version = c.get('/api/templates/old.typ').json()['version']
    assert _res(c, "old.typ", "Newer edit").status_code == 200
    assert c.post('/api/templates/old.typ/rename', json={"name": "new.typ", "version": version}).status_code == 409
    assert c.get('/api/templates/old.typ').json()['content'] == 'Newer edit'
    assert c.get('/api/templates/new.typ').status_code == 404


def _typst_available():
    import shutil
    return shutil.which("typst") is not None


def test_export_zip_includes_all_kinds(c):
    register_user(c, "usr")
    make_doc(c, title="D", content="hi")
    _res(c, "a.typ", "#x")
    _res(c, "r.bib", "@b")
    _res(c, "d.csv", "a")
    r = c.get("/api/export.zip")
    assert r.status_code == 200, r.text
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "templates/a.typ" in names
    assert "templates/r.bib" in names
    assert "templates/d.csv" in names


def test_export_doc_merges_owner_resources(c):
    if not _typst_available():
        return
    register_user(c, "own")
    did = make_doc(c, title="Exp", content='#include "part.typ"\nHello')
    _res(c, "part.typ", "PART")
    _snapshot(c, did, "part.typ")
    register_user(c, "viw")
    login(c, "own")
    c.post(f"/api/docs/{did}/share", json={"username": "viw", "role": "editor"})
    login(c, "viw")
    r = c.post(f"/api/docs/{did}/export?format=zip")
    assert r.status_code == 200, r.text
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "Exp.typ" in names
    assert "part.typ" in names  # owner's resource merged at root, viewer's shelf untouched
    assert c.get("/api/templates").json()["templates"] == []


def test_reserved_main_typ_rejected(c):
    from backend import db as _db

    register_user(c, "usr")
    assert _res(c, "__doc__.typ", "x").status_code == 400
    assert _res(c, "__DOC__.TYP", "x").status_code == 400
    _res(c, "a.typ", "A")
    assert c.post("/api/templates/a.typ/duplicate", json={"name": "__doc__.typ"}).status_code == 400
    # Pre-existing main.typ rows stay readable/deletable (cleanup path).
    con = _db.connect()
    try:
        con.execute("INSERT INTO templates (owner, name, content, line, folder, updated_at) "
                    "VALUES (?,?,?,?,?,?)", ("usr", "__doc__.typ", "OLD", "", "", _db.now_iso()))
        con.commit()
    finally:
        con.close()
    assert c.get("/api/templates/__doc__.typ").status_code == 200
    assert _res(c, "__doc__.typ", "new").status_code == 400  # updates rejected too
    assert c.delete("/api/templates/__doc__.typ").status_code == 200


def test_insert_line_case_insensitive():
    from backend.services import resources as _r

    assert _r.insert_line("r.BIB") == '#bibliography("r.BIB")'
    assert _r.insert_line("t.TYP") == '#include "t.TYP"'
    assert _r.insert_line("d.CSV") == '#csv("d.CSV")'


def test_effective_includes_versions(c):
    from backend.services import project_files

    register_user(c, "own")
    _res(c, "a.typ", "A")
    did = make_doc(c, title="D", content='#include "a.typ"')
    r = c.get(f"/api/docs/{did}/resources")
    assert r.status_code == 200
    assert r.json()["files"] == [{"path": "a.typ", "size": 1, "version": project_files.version(b"A")}]


def test_nested_relative_resolution(c):
    register_user(c, "own")
    did = make_doc(c, title="N", content='#include "sub/part.typ"\nHi')
    pid = project_of(c, did)
    assert put_file(c, pid, "sub/part.typ", b'#include "leaf.typ"\nPART').status_code == 200
    put_file(c, pid, "sub/leaf.typ", b"LEAF")
    put_file(c, pid, "leaf.typ", b"ROOT")
    body = c.get(f"/api/docs/{did}/resources").json()
    assert sorted(f["path"] for f in body["files"]) == ["sub/leaf.typ", "sub/part.typ"]  # relative to the including file


def test_quoted_prose_only_counts_when_it_names_a_file():
    from backend.services import resources as _r

    assert _r.reachable({"a.typ"}, {}, "", 'say "hello world" and "see this" #include "a.typ"') == {"a.typ"}


def test_version_missing_row_404(c):
    register_user(c, "usr")
    r = c.post("/api/templates", json={"name": "ghost.typ", "content": "x", "version": "stale"})
    assert r.status_code == 404


def test_duplicate_quota_rollback(c, monkeypatch):
    from backend.services import quota as _q

    register_user(c, "usr")
    _res(c, "a.typ", "A")
    calls = {"n": 0}

    def _fake_bytes(owner):
        calls["n"] += 1
        return 0 if calls["n"] == 1 else 10 ** 12

    monkeypatch.setattr(_q, "user_bytes", _fake_bytes)
    r = c.post("/api/templates/a.typ/duplicate", json={"name": "b.typ"})
    assert r.status_code == 413
    names = {t["name"] for t in c.get("/api/templates").json()["templates"]}
    assert "b.typ" not in names


def test_csv_contents_do_not_expand(c):
    register_user(c, "own")
    did = make_doc(c, title="D", content='#csv("data.csv")')
    pid = project_of(c, did)
    put_file(c, pid, "data.csv", b'a,b\n"x.typ",2')
    put_file(c, pid, "x.typ", b"PHANTOM")
    body = c.get(f"/api/docs/{did}/resources").json()
    assert [f["path"] for f in body["files"]] == ["data.csv"]


def test_resource_can_be_cleared_and_reopened(c):
    register_user(c, "empty_resource")
    for name, content in [("table.csv", "a,b\n1,2\n"), ("refs.bib", "@book{k,}"), ("layout.typ", "Text")]:
        created = _res(c, name, content).json()
        saved = _res(c, name, "", version=created["version"])
        assert saved.status_code == 200
        assert c.get("/api/templates/" + name).json()["content"] == ""


def test_data_formats_are_global_local_and_exportable(c):
    register_user(c, "data_formats")
    samples = {"tsv": "name\tvalue\na\t1\n", "json": '{"title":"Data"}', "yaml": "title: Data\n", "yml": "title: Data\n", "toml": 'title = "Data"\n', "xml": "<data>Data</data>", "txt": "Data", "csl": '<style xmlns="http://purl.org/net/xbiblio/csl"/>'}
    refs = []
    for ext, text in samples.items():
        name = "global." + ext
        assert _res(c, name, text).status_code == 200
        assert c.get("/api/templates/" + name).json()["content"] == text
        refs.append('#let file_' + ext + ' = "' + name + '"')
    did = make_doc(c, title="Formats", content="\n".join(refs))
    pid = project_of(c, did)
    for ext, text in samples.items():
        name = "local." + ext
        assert put_file(c, pid, name, text.encode()).status_code == 200
        assert c.get(f"/api/projects/{pid}/files/{name}").text == text
    exported = c.post(f"/api/docs/{did}/export?format=zip")
    assert exported.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(exported.content))
    for ext, text in samples.items():
        assert archive.read("global." + ext).decode() == text
        assert archive.read("local." + ext).decode() == text
