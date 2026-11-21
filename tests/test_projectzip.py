import io
import json
import zipfile

import pytest
from conftest import make_doc, project_of, put_file, register_user


def bundle(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


def import_zip(c, data, documents=(), name="Imported.zip"):
    return c.post("/api/docs/import-zip", files={"f": (name, data, "application/zip")}, data={"name": name, "documents": json.dumps(list(documents))})


def files_of(c, pid):
    return {f["path"]: c.get(f"/api/projects/{pid}/files/{f['path']}").content for f in c.get(f"/api/projects/{pid}/files").json()["files"]}


def test_export_import_round_trip_keeps_the_tree(c):
    register_user(c, "zipuser")
    assert c.post("/api/templates", json={"name": "layout.typ", "content": '= Global layout'}).status_code == 200
    assert c.post("/api/templates", json={"name": "values.json", "content": '{"amount":12}'}).status_code == 200
    main = '#include "layout.typ"\n#json("values.json").amount\n#include "chapters/chapter.typ"'
    did = make_doc(c, title="Original", content=main)
    assert put_file(c, project_of(c, did), "chapters/chapter.typ", b"Nested chapter").status_code == 200
    exported = c.post(f"/api/docs/{did}/export?format=zip")
    assert exported.status_code == 200
    result = import_zip(c, exported.content, ["Original.typ"], name="Archive.zip")
    assert result.status_code == 200, result.text
    document = c.get(f"/api/docs/{result.json()['id']}").json()
    assert document["content"] == main
    assert document["folder"] == ""
    assert c.get(f"/api/projects/{document['project_id']}").json()["title"] == "Archive"
    assert files_of(c, document["project_id"]) == {"layout.typ": b"= Global layout", "values.json": b'{"amount":12}', "chapters/chapter.typ": b"Nested chapter"}
    # Import is self-contained even when the global shelf changes later.
    assert c.delete("/api/templates/layout.typ").status_code == 200
    assert files_of(c, document["project_id"])["layout.typ"] == b"= Global layout"


@pytest.mark.parametrize("entries,documents", [
    ([("main.typ", "Main"), ("../bad.typ", "Bad")], []),
    ([("main.typ", "Main"), ("/bad.typ", "Bad")], []),
    ([("main.typ", "Main"), ("chapter.typ", "A"), ("CHAPTER.typ", "B")], []),
    ([("main.typ", b"\xff")], []),
    ([("main.typ", "Main")], ["missing.typ"]),
])
def test_invalid_archives_do_not_create_anything(c, entries, documents):
    register_user(c, "zipbad")
    before = c.get("/api/projects").json()["projects"]
    result = import_zip(c, bundle(entries), documents)
    assert result.status_code == 400
    assert c.get("/api/projects").json()["projects"] == before


def test_zip_paths_stay_and_a_second_import_makes_a_second_project(c):
    register_user(c, "zipfolder")
    data = bundle([("Project/main.typ", '#include "part.typ"'), ("Project/part.typ", "Part")])
    first = import_zip(c, data, ["Project/main.typ"])
    assert first.status_code == 200, first.text
    doc = c.get(f"/api/docs/{first.json()['id']}").json()
    assert doc["folder"] == "Project"
    assert files_of(c, first.json()["project_id"]) == {"Project/part.typ": b"Part"}
    second = import_zip(c, data, ["Project/main.typ"])
    assert second.status_code == 200
    assert second.json()["project_id"] != first.json()["project_id"]
    titles = sorted(p["title"] for p in c.get("/api/projects").json()["projects"])
    assert "Imported" in titles and "Imported (2)" in titles


def test_zip_import_rolls_back_when_storage_write_fails(c, monkeypatch):
    from backend.services.docfiles import store

    register_user(c, "zipfail")
    before = c.get("/api/projects").json()["projects"]
    original = store.put

    def fail_second(key, name, data):
        if name == "b.typ":
            raise OSError("Disk full")
        return original(key, name, data)

    monkeypatch.setattr(store, "put", fail_second)
    with pytest.raises(OSError):
        import_zip(c, bundle([("main.typ", '#include "a.typ"\n#include "b.typ"'), ("a.typ", "A"), ("b.typ", "B")]), ["main.typ"])
    monkeypatch.setattr(store, "put", original)
    assert c.get("/api/projects").json()["projects"] == before


def test_source_zip_preserves_generic_binary_files(c):
    register_user(c, "zipgeneric")
    result = import_zip(c, bundle([("main.typ", "Main"), ("assets/data.bin", b"\x00\xff")]), ["main.typ"])
    assert result.status_code == 200, result.text
    response = c.get(f"/api/projects/{result.json()['project_id']}/files/assets/data.bin")
    assert response.content == b"\x00\xff"
    assert response.headers["content-disposition"].startswith("attachment;")


def test_project_is_named_after_the_zip_file(c):
    register_user(c, "zipname")
    result = import_zip(c, bundle([("main.typ", "= Hi")]), ["main.typ"], name="Übung 1.zip")
    assert result.status_code == 200, result.text
    assert c.get(f"/api/projects/{result.json()['project_id']}").json()["title"] == "Übung 1"


def test_zip_preview_suggests_roots_and_import_takes_any_number_of_documents(c):
    register_user(c, "zipchoose")
    data = bundle([("book.typ", '#include "ch1.typ"'), ("ch1.typ", "One"), ("notes.typ", "Notes")])
    preview = c.post("/api/zip-preview", files={"f": ("s.zip", data, "application/zip")}).json()
    assert preview == {"entries": ["book.typ", "ch1.typ", "notes.typ"], "suggested": ["book.typ", "notes.typ"]}
    none = import_zip(c, data, [])
    assert none.status_code == 200, none.text
    assert none.json()["documents"] == [] and set(files_of(c, none.json()["project_id"])) == {"book.typ", "ch1.typ", "notes.typ"}
    many = import_zip(c, data, ["book.typ", "notes.typ"])
    assert many.status_code == 200, many.text
    project = c.get(f"/api/projects/{many.json()['project_id']}").json()
    assert sorted(d["title"] for d in project["documents"]) == ["book", "notes"]
    assert [f["path"] for f in project["files"]] == ["ch1.typ"]
    assert c.post("/api/docs/import-zip", files={"f": ("s.zip", data)}, data={"documents": "not json"}).status_code == 400
