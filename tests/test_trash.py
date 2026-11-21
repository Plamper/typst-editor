from conftest import login, make_doc, project_of, put_file, register_user

from backend import db
from backend import main as backend_main
from backend.services import quota


def _upload_and_delete(c, did, name, data):
    pid = project_of(c, did)
    v = put_file(c, pid, name, data).json()["version"]
    assert c.delete(f"/api/projects/{pid}/files/{name}", params={"version": v}).status_code == 200
    return pid


def test_listing_never_shows_an_uncommitted_restore(c, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from backend.services.docfiles import store

    register_user(c, "alice")
    did = make_doc(c, "Restore race")
    pid = _upload_and_delete(c, did, "race.txt", b"restored")
    entry = _trash_for(c, "race.txt")[0]
    published, resume = threading.Event(), threading.Event()
    put = store.put

    def publish(*args):
        put(*args)
        published.set()
        assert resume.wait(10)

    monkeypatch.setattr(store, "put", publish)
    with ThreadPoolExecutor(2) as pool:
        restore = pool.submit(c.post, f"/api/trash/{entry['id']}/restore")
        try:
            assert published.wait(10)
            # Bytes are on disk, the row is not committed: the listing reads rows only.
            assert c.get(f"/api/projects/{pid}/files").json()["files"] == []
        finally:
            resume.set()
        response = restore.result(timeout=10)
        assert response.status_code == 200, response.text
    assert c.get(f"/api/projects/{pid}/files").json()["files"][0]["path"] == "race.txt"


def _trash_for(c, title):
    return [t for t in c.get("/api/docs").json()["trash"] if t["title"] == title and t.get("kind") == "file"]


def test_delete_twice_gone(c):
    register_user(c, "alice")
    did = make_doc(c, "Papierkorb")
    assert c.delete(f"/api/docs/{did}").json()["trashed"] is True
    assert c.delete(f"/api/docs/{did}").json()["trashed"] is False
    assert c.get(f"/api/docs/{did}").status_code == 404


def test_trashed_410(c):
    register_user(c, "alice")
    did = make_doc(c, "Papierkorb2")
    c.delete(f"/api/docs/{did}")
    assert c.get(f"/api/docs/{did}").status_code == 410
    assert c.post(f"/api/docs/{did}/save", json={"content": "x"}).status_code == 410


def test_restore(c):
    register_user(c, "alice")
    did = make_doc(c, "Papierkorb3")
    c.delete(f"/api/docs/{did}")
    assert c.post(f"/api/docs/{did}/restore").status_code == 200
    assert c.get(f"/api/docs/{did}").status_code == 200


def test_foreign_restore_403_404(c):
    register_user(c, "alice")
    register_user(c, "bob")
    login(c, "alice")
    did = make_doc(c, "Papierkorb4")
    login(c, "bob")
    assert c.post(f"/api/docs/{did}/restore").status_code == 404  # need_access hides existence


def test_reap_trash_dirs(c):
    register_user(c, "alice")
    keep, gone, both = (c.get(f"/api/docs/{make_doc(c, t)}").json()["project_id"] for t in ("ReapKeep", "ReapGone", "ReapBoth"))
    fdir = backend_main.get_files_dir()
    fdir.mkdir(parents=True, exist_ok=True)
    # Crash before commit: project row exists, live dir missing -> move back.
    live = fdir / keep
    live.mkdir(parents=True, exist_ok=True)
    (live / "a.png").write_bytes(b"data-keep")
    live.rename(fdir / f".trash-{keep}")
    # Crash after commit: no project row -> rmtree.
    live = fdir / gone
    live.mkdir(parents=True, exist_ok=True)
    (live / "b.png").write_bytes(b"data-gone")
    live.rename(fdir / f".trash-{gone}")
    con = db.connect()
    try:
        con.execute("DELETE FROM docs WHERE project_id=?", (gone,))
        con.execute("DELETE FROM projects WHERE id=?", (gone,))
        con.commit()
    finally:
        con.close()
    # Ambiguous: project row and live dir both present -> leave + warn.
    live = fdir / both
    live.mkdir(parents=True, exist_ok=True)
    (live / "live.png").write_bytes(b"live")
    stale = fdir / f".trash-{both}"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "stale.png").write_bytes(b"stale")

    assert backend_main.reap_trash_dirs() == {"restored": 1, "removed": 1, "skipped": 1}
    assert (fdir / keep / "a.png").read_bytes() == b"data-keep"
    assert not (fdir / f".trash-{keep}").exists()
    assert not (fdir / f".trash-{gone}").exists()
    assert (fdir / both / "live.png").read_bytes() == b"live"
    assert (stale / "stale.png").read_bytes() == b"stale"


def test_drag_to_trash_is_idempotent_and_restorable(c):
    register_user(c, "alice")
    did = make_doc(c, "Drag trash")
    for _ in range(2):
        response = c.post(f"/api/docs/{did}/trash")
        assert response.status_code == 200
        assert response.json() == {"trashed": True}
    assert c.get(f"/api/docs/{did}").status_code == 410
    assert c.post(f"/api/docs/{did}/restore").status_code == 200
    assert c.get(f"/api/docs/{did}").json()["title"] == "Drag trash"


def test_drag_to_trash_cannot_delete_someone_elses_document(c):
    register_user(c, "alice")
    did = make_doc(c, "Private drag")
    register_user(c, "bob")
    assert c.post(f"/api/docs/{did}/trash").status_code == 404
    login(c, "alice")
    assert c.get(f"/api/docs/{did}").status_code == 200


def test_restore_file_quota_neutral_at_cap(c, monkeypatch):
    register_user(c, "alice")
    login(c, "alice")
    did = make_doc(c, "RestoreCap")
    pid = _upload_and_delete(c, did, "big.bin", b"0123456789")
    used = quota.user_bytes("alice")  # includes the 10 bytes parked in trash
    monkeypatch.setattr(quota, "quota_cap", lambda: used)
    entry = _trash_for(c, "big.bin")[0]
    # Restore is quota-neutral: the bytes leave the trash as they land in the tree.
    assert c.post(f"/api/trash/{entry['id']}/restore").status_code == 200, None
    assert c.get(f"/api/projects/{pid}/files/big.bin").content == b"0123456789"


def test_file_trash_outlives_documents_until_the_project_goes(c):
    register_user(c, "alice")
    login(c, "alice")
    did = make_doc(c, "Endgueltig")
    pid = _upload_and_delete(c, did, "note.txt", b"data")
    assert c.delete(f"/api/docs/{did}").json()["trashed"] is True
    assert c.delete(f"/api/docs/{did}").json()["trashed"] is False
    assert _trash_for(c, "note.txt")  # the file belonged to the project, which still exists
    c.post(f"/api/projects/{pid}/docs", json={"title": "Last"})
    assert c.delete(f"/api/projects/{pid}").status_code == 200
    c.delete("/api/trash")
    assert _trash_for(c, "note.txt") == []


def test_restore_after_hard_deleted_project_410(c):
    register_user(c, "alice")
    login(c, "alice")
    did = make_doc(c, "Geist")
    pid = _upload_and_delete(c, did, "ghost.txt", b"x")
    entry = _trash_for(c, "ghost.txt")[0]
    con = db.connect()
    try:
        con.execute("DELETE FROM docs WHERE project_id=?", (pid,))
        con.execute("DELETE FROM projects WHERE id=?", (pid,))
        con.commit()
    finally:
        con.close()
    r = c.post(f"/api/trash/{entry['id']}/restore")
    assert r.status_code == 410, r.text


def test_project_hard_delete_purges_file_trash(c):
    register_user(c, "alice")
    login(c, "alice")
    pid = c.post("/api/projects", json={"title": "Bibliothek"}).json()["id"]
    v = put_file(c, pid, "vals.typ", b"x=1\n").json()["version"]
    assert c.delete(f"/api/projects/{pid}/files/vals.typ", params={"version": v}).status_code == 200
    assert _trash_for(c, "vals.typ")
    # no documents ever existed: the project hard-deletes with its files,
    # so the archived copy could never land anywhere again
    assert c.delete(f"/api/projects/{pid}").json()["ok"] is True
    assert _trash_for(c, "vals.typ") == []


def test_empty_trash_removes_twelve_documents_and_mixed_entries_in_one_request(c, monkeypatch):
    from backend import deps
    from backend.services import trash

    register_user(c, "bob")
    foreign = make_doc(c, "Bob trash")
    register_user(c, "alice")
    keep = make_doc(c, "Keep active")
    documents = [make_doc(c, f"Trash {i}") for i in range(12)]
    keep_pid, gone = project_of(c, keep), project_of(c, documents[0])
    with db.tx() as con:
        for document in documents + [foreign]:
            con.execute("UPDATE docs SET trashed=1 WHERE id=?", (document,))
        for i in range(12):
            trash.archive(con, "alice", ["personal", "file"][i % 2], f"trash{i}.typ", b"old", project_id=keep_pid)
        foreign_entry = trash.archive(con, "bob", "personal", "foreign.typ", b"keep")
    with db.tx() as con:
        con.execute("UPDATE projects SET archived=1 WHERE id=?", (gone,))
    folder = deps.get_files_dir() / gone
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "image.png").write_bytes(b"image")
    scopes = []
    limited = deps.limited

    def counted(req, scope, key=""):
        scopes.append(scope)
        return limited(req, scope, key)

    monkeypatch.setattr(deps, "limited", counted)
    response = c.delete("/api/trash")
    assert response.status_code == 200, response.text
    assert set(response.json()["documents"]) == set(documents)
    assert scopes == ["delete"]
    assert not folder.exists()
    with db.tx() as con:
        assert con.execute("SELECT COUNT(*) FROM docs WHERE owner='alice' AND trashed=1").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM trash_entries WHERE owner='alice'").fetchone()[0] == 0
        assert con.execute("SELECT trashed FROM docs WHERE id=?", (keep,)).fetchone()[0] == 0
        assert con.execute("SELECT trashed FROM docs WHERE id=?", (foreign,)).fetchone()[0] == 1
        assert con.execute("SELECT id FROM trash_entries WHERE id=?", (foreign_entry,)).fetchone()
    assert c.delete("/api/trash").json() == {"documents": []}


def test_file_restore_with_stale_checkpoint_does_not_deadlock(c):
    import threading

    register_user(c, "alice")
    did = c.post("/api/docs/create", json={"title": "Restore deadlock", "content": "x"}).json()["id"]
    pid = _upload_and_delete(c, did, "pic.png", b"png")
    entry = _trash_for(c, "pic.png")[0]
    con = db.connect()
    try:
        con.execute("UPDATE project_snapshots SET created_at='2020-01-01T00:00:00+00:00' WHERE project_id=?", (pid,))
        con.commit()
    finally:
        con.close()
    out = {}

    def worker():
        out["r"] = c.post(f"/api/trash/{entry['id']}/restore")

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "trash restore deadlocked on its own files lock"
    assert out["r"].status_code == 200, out["r"].text
    assert c.get(f"/api/projects/{pid}/files/pic.png").content == b"png"
