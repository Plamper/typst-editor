"""Migration v34: modules and attachments become one project file tree, losslessly and idempotently."""
import json

from conftest import register_user
from pycrdt import Doc, Text

from backend import db, deps
from backend.services import project_files


def _old_layout(c):
    """A v33 project: module lib/style.typ, doc A (top level) and doc B (folder Ch) with attachments."""
    register_user(c, "alice")
    a = c.post("/api/docs/create", json={"title": "A", "content": ""}).json()["id"]
    pid = c.get(f"/api/docs/{a}").json()["project_id"]
    b = c.post(f"/api/projects/{pid}/docs", json={"title": "B", "folder": "Ch"}).json()["id"]
    ydoc = Doc()
    ydoc.get("typst", type=Text).insert(0, '#import "lib/style.typ"\n#image("logo.png")\n#include "part.typ"')
    texts = {
        a: '#import "lib/style.typ"\n#image("Ch/logo.png")\n#image("logo.png")\n#image("missing.png")',
        b: str(ydoc.get("typst", type=Text)),
    }
    root = deps.get_files_dir()
    with db.tx() as con:
        con.execute("CREATE TABLE files (doc_id TEXT NOT NULL REFERENCES docs(id) ON DELETE CASCADE, path TEXT NOT NULL, "
                    "size INTEGER NOT NULL DEFAULT 0, mtime REAL NOT NULL DEFAULT 0, type TEXT NOT NULL DEFAULT '', PRIMARY KEY (doc_id, path))")
        con.execute("CREATE TABLE project_resources (project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, "
                    "name TEXT NOT NULL, content TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL, PRIMARY KEY(project_id, name))")
        con.execute("INSERT INTO project_resources VALUES (?,?,?,?)", (pid, "lib/style.typ", '#let x = read("../data.csv")', db.now_iso()))
        con.execute("INSERT INTO project_resources VALUES (?,?,?,?)", (pid, "data.csv", "a,b", db.now_iso()))
        con.execute("UPDATE docs SET content=? WHERE id=?", (texts[a], a))
        con.execute("UPDATE docs SET content=?,yjs=? WHERE id=?", (texts[b], ydoc.get_update(), b))
        for did, path, data in ((a, "Ch/logo.png", b"A-logo"), (a, "logo.png", b"A-root"), (b, "logo.png", b"B-logo"),
                                (b, "part.typ", b'#image("logo.png")')):
            (root / did / path).parent.mkdir(parents=True, exist_ok=True)
            (root / did / path).write_bytes(data)
            con.execute("INSERT INTO files VALUES (?,?,?,?,?)", (did, path, len(data), 0, ""))
        con.execute("INSERT INTO trash_entries (id,owner,kind,project_id,doc_id,name,content,created_at) VALUES ('t1','alice','file',?,?,'gone.png',x'00',?)", (pid, b, db.now_iso()))
        con.execute("INSERT INTO trash_entries (id,owner,kind,project_id,doc_id,name,content,created_at) VALUES ('t2','alice','resource',?,'','old.typ','x',?)", (pid, db.now_iso()))
        files = [{"id": "f1", "kind": "file", "doc_id": b, "path": "part.typ", "action": "modify", "base": "x", "content": "y", "changes": []},
                 {"id": "f2", "kind": "resource", "path": "data.csv", "action": "delete", "base": "a,b"},
                 {"id": "f3", "kind": "document", "doc_id": a, "path": "", "action": "modify", "base": "", "content": "", "changes": []}]
        con.execute("INSERT INTO suggestion_packages(id,project_id,author,title,files,created_at,updated_at) VALUES ('sp1',?,'alice','T',?,?,?)",
                    (pid, json.dumps(files), db.now_iso(), db.now_iso()))
        con.execute("DELETE FROM schema_version WHERE v=34")
    return pid, a, b


def _run():
    con = db.connect()
    try:
        db.migrate(con)
    finally:
        con.close()


def test_migration_moves_files_rewrites_references_and_converts_entries(c):
    pid, a, b = _old_layout(c)
    _run()
    root = deps.get_files_dir()
    con = db.connect()
    try:
        tree = {r[0]: r[1] for r in con.execute("SELECT path,version FROM project_files WHERE project_id=?", (pid,))}
        assert set(tree) == {"lib/style.typ", "data.csv", "Ch/logo.png", "logo.png", "Ch/logo-2.png", "Ch/part.typ"}
        for path, data in (("Ch/logo.png", b"A-logo"), ("logo.png", b"A-root"), ("Ch/logo-2.png", b"B-logo")):
            assert (root / pid / path).read_bytes() == data and tree[path] == project_files.version(data)
        # B's attachment logo.png clashed with A's Ch/logo.png: B's texts now name the new file absolutely.
        assert (root / pid / "Ch/part.typ").read_bytes() == b'#image("/Ch/logo-2.png")'
        assert (root / pid / "lib/style.typ").read_bytes() == b'#let x = read("../data.csv")'
        doc_a = con.execute("SELECT content FROM docs WHERE id=?", (a,)).fetchone()[0]
        assert doc_a == '#import "lib/style.typ"\n#image("Ch/logo.png")\n#image("logo.png")\n#image("missing.png")'
        row = con.execute("SELECT content,yjs FROM docs WHERE id=?", (b,)).fetchone()
        want = '#import "/lib/style.typ"\n#image("/Ch/logo-2.png")\n#include "part.typ"'
        assert row["content"] == want and project_files.stored_text("", bytes(row["yjs"])) == want
        assert con.execute("SELECT label FROM snapshots WHERE doc_id=?", (b,)).fetchone()[0] == "project-files migration"
        assert not con.execute("SELECT 1 FROM snapshots WHERE doc_id=?", (a,)).fetchone()
        trash = {r["id"]: dict(r) for r in con.execute("SELECT id,kind,project_id,doc_id,name FROM trash_entries")}
        assert trash["t1"] == {"id": "t1", "kind": "file", "project_id": pid, "doc_id": "", "name": "Ch/gone.png"}
        assert trash["t2"]["kind"] == "file" and trash["t2"]["name"] == "old.typ"
        files = json.loads(con.execute("SELECT files FROM suggestion_packages WHERE id='sp1'").fetchone()[0])
        assert files[0]["kind"] == "file" and files[0]["path"] == "Ch/part.typ" and "doc_id" not in files[0]
        assert files[1]["kind"] == "file" and files[1]["path"] == "data.csv"
        assert files[2]["kind"] == "document" and files[2]["doc_id"] == a
        assert not {"files", "project_resources"} & {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    assert not (root / a).exists() and not (root / b).exists()
    # Restored trash entry lands in the tree; the proposal still reads its file.
    assert c.post("/api/trash/t1/restore").status_code == 200
    assert c.get(f"/api/projects/{pid}/files/Ch/gone.png").content == b"\x00"
    assert c.get(f"/api/projects/{pid}/proposals/sp1").status_code == 200


def test_migration_is_idempotent_after_a_crash(c, monkeypatch):
    pid, a, b = _old_layout(c)
    from backend import db as dbmod

    real = dbmod._migrate_v34

    def crash(con):
        real(con)
        raise RuntimeError("crash before commit")

    monkeypatch.setattr(dbmod, "_MIGRATIONS", dbmod._MIGRATIONS[:-1] + (crash,))
    try:
        _run()
    except RuntimeError:
        pass
    monkeypatch.setattr(dbmod, "_MIGRATIONS", dbmod._MIGRATIONS[:-1] + (real,))
    _run()
    _run()
    con = db.connect()
    try:
        paths = sorted(r[0] for r in con.execute("SELECT path FROM project_files WHERE project_id=?", (pid,)))
        assert paths == ["Ch/logo-2.png", "Ch/logo.png", "Ch/part.typ", "data.csv", "lib/style.typ", "logo.png"]
        assert con.execute("SELECT COUNT(*) FROM schema_version WHERE v=34").fetchone()[0] == 1
    finally:
        con.close()
    assert (deps.get_files_dir() / pid / "Ch/logo-2.png").read_bytes() == b"B-logo"


def test_old_project_checkpoint_restores(c):
    import base64

    register_user(c, "alice")
    a = c.post("/api/docs/create", json={"title": "A", "content": "now"}).json()["id"]
    pid = c.get(f"/api/docs/{a}").json()["project_id"]
    old = {"documents": [{"id": a, "title": "A", "folder": "Ch", "content": '#image("logo.png")\n#include "m.typ"',
                          "files": {"logo.png": base64.b64encode(b"L").decode()}}],
           "resources": {"m.typ": "M", "Ch/logo.png": "clash"}}
    with db.tx() as con:
        con.execute("INSERT INTO project_snapshots (id,project_id,content,label,created_at) VALUES ('ps1',?,?,'old',?)", (pid, json.dumps(old), db.now_iso()))
    assert c.get(f"/api/projects/{pid}/snapshots/ps1").status_code == 200
    r = c.post(f"/api/projects/{pid}/snapshots/ps1/restore", json={"force": True})
    assert r.status_code == 200, r.text
    assert c.get(f"/api/projects/{pid}/files/Ch/logo-2.png").content == b"L"
    assert c.get(f"/api/projects/{pid}/files/m.typ").content == b"M"
    assert c.get(f"/api/docs/{a}").json()["content"] == '#image("/Ch/logo-2.png")\n#include "/m.typ"'
