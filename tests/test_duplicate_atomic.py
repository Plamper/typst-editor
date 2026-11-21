"""Duplicate: a copy of the document in the same project folder; project files stay shared.

Edge matrix: content and title copied, files neither copied nor charged twice,
stale staging dirs from older versions are swept at startup, sequential
duplicates de-conflict titles, reviewers cannot duplicate.
"""
from conftest import login, make_doc, project_of, put_file, register_user

import backend.main as main


def _files_dir():
    return main.get_files_dir()


def test_duplicate_copies_content_and_shares_files(c):
    register_user(c, "alice")
    login(c, "alice")
    did = make_doc(c, "Original", content='hello dup #image("img.png")')
    pid = project_of(c, did)
    put_file(c, pid, "img.png", b"\x89PNG-abc")
    r = c.post(f"/api/docs/{did}/duplicate")
    assert r.status_code == 200, r.text
    nid = r.json()["id"]
    assert nid != did
    got = c.get(f"/api/docs/{nid}").json()
    assert got["content"] == 'hello dup #image("img.png")'
    assert (got["title"], got["project_id"]) == ("Original (copy)", pid)
    assert [f["path"] for f in c.get(f"/api/docs/{nid}/resources").json()["files"]] == ["img.png"]
    assert not (_files_dir() / nid).exists()


def test_duplicate_titles_deconflict(c):
    register_user(c, "alice")
    login(c, "alice")
    did = make_doc(c, "Deconflict")
    n1 = c.post(f"/api/docs/{did}/duplicate").json()["id"]
    n2 = c.post(f"/api/docs/{did}/duplicate").json()["id"]
    t1 = c.get(f"/api/docs/{n1}").json()["title"]
    t2 = c.get(f"/api/docs/{n2}").json()["title"]
    assert (t1, t2) == ("Deconflict (copy)", "Deconflict (copy) 2")


def test_stale_staging_swept_at_startup(c):
    stale = _files_dir() / ".tmp.dup-d_stale123"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "orphan.bin").write_bytes(b"z")
    counts = main.reap_trash_dirs()
    assert counts["removed"] >= 1
    assert not stale.exists()


def test_duplicate_reviewer_forbidden(c):
    register_user(c, "alice")
    register_user(c, "bob")
    login(c, "alice")
    did = make_doc(c, "NoDupReview")
    assert c.post(f"/api/docs/{did}/share", json={"username": "bob", "role": "reviewer"}).status_code == 200
    login(c, "bob")
    assert c.post(f"/api/docs/{did}/duplicate").status_code == 403
    login(c, "alice")
    assert c.post(f"/api/docs/{did}/duplicate").status_code == 200  # owner ok
