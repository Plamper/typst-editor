"""Round 4 fixes: checkpoint retention (#5), stale selections instead of hard
409 (#4), project-unique entry names (#7) and the open-packages summary (#6)."""
import datetime
import io
import zipfile

from conftest import login, make_doc, put_file, register_user

from backend import constants, db
from backend.services import project_history


def project(c, content="base"):
    """Registers alice, creates doc "Round4"; returns (project_id, doc_id)."""
    register_user(c, "alice")
    login(c, "alice")
    did = make_doc(c, "Round4", content)
    pid = c.get(f"/api/docs/{did}").json()["project_id"]
    return pid, did


def member(c, pid, username, role="reviewer"):
    """The project owner (already logged in) adds a member."""
    r = c.post(f"/api/projects/{pid}/members", json={"username": username, "role": role})
    assert r.status_code == 200, r.text


# --- #5 smart checkpoint retention ------------------------------------------

def _snapshots(pid):
    with db.tx() as con:
        return con.execute(
            "SELECT id,kind,label,created_at FROM project_snapshots WHERE project_id=? "
            "ORDER BY created_at DESC,rowid DESC", (pid,)).fetchall()


def _every_change(pid, keep=constants.AUTO_SNAP_KEEP):
    """No minimum gap between automatic versions, so every changed state is saved."""
    with db.tx() as con:
        con.execute("INSERT INTO project_versioning (project_id,auto_minutes,auto_keep) VALUES (?,0,?)", (pid, keep))


def _edit(did, text):
    with db.tx() as con:
        con.execute("UPDATE docs SET content=? WHERE id=?", (text, did))


def test_auto_checkpoints_stay_bounded_and_manuals_survive(c):
    pid, did = project(c)
    assert c.post(f"/api/projects/{pid}/snapshots", json={"label": "Manual"}).status_code == 200
    assert c.post(f"/api/projects/{pid}/snapshots", json={}).status_code == 200  # UI manual save (empty label)
    base = _snapshots(pid)
    manuals = {r["id"] for r in base if r["kind"] == "manual"}
    assert len(base) == 3 and len(manuals) == 2  # creation anchor (auto) + both manuals
    _every_change(pid, keep=5)
    for n in range(12):
        _edit(did, f"v{n:03d}")
        project_history.auto(pid)
    rows = _snapshots(pid)
    autos = [r for r in rows if r["kind"] == "auto"]
    assert {r["id"] for r in rows if r["kind"] == "manual"} == manuals  # never pruned
    assert len(autos) == 5 + 1  # the newest five plus the creation anchor


def test_unchanged_project_gets_no_automatic_version(c):
    pid, did = project(c)
    _every_change(pid)
    for _ in range(3):
        project_history.auto(pid)
    assert len(_snapshots(pid)) == 1  # only the creation anchor
    _edit(did, "changed")
    project_history.auto(pid)
    project_history.auto(pid)
    assert len(_snapshots(pid)) == 2


def test_minimum_gap_between_automatic_versions(c):
    pid, did = project(c)
    with db.tx() as con:  # the creation anchor is an hour old, the gap is two hours
        con.execute("INSERT INTO project_versioning (project_id,auto_minutes) VALUES (?,120)", (pid,))
        hour_ago = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)).isoformat()
        con.execute("UPDATE project_snapshots SET created_at=? WHERE project_id=?", (hour_ago, pid))
    _edit(did, "changed")
    project_history.auto(pid)
    assert len(_snapshots(pid)) == 1
    with db.tx() as con:
        con.execute("UPDATE project_versioning SET auto_minutes=30 WHERE project_id=?", (pid,))
    project_history.auto(pid)
    assert len(_snapshots(pid)) == 2


def test_creation_anchor_and_newest_auto_survive_pruning(c):
    pid, did = project(c)
    anchor = _snapshots(pid)[0]["id"]  # the one row so far: the creation checkpoint
    _every_change(pid, keep=3)
    for n in range(8):
        _edit(did, f"v{n:03d}")
        project_history.auto(pid)
    rows = _snapshots(pid)
    assert anchor in {r["id"] for r in rows}
    newest = rows[0]
    assert newest["kind"] == "auto"
    _edit(did, "later")
    # Restoring the newest kept checkpoint keeps working after pruning.
    response = c.post(f"/api/projects/{pid}/snapshots/{newest['id']}/restore", json={})
    assert response.status_code == 200, response.text
    assert c.get(f"/api/docs/{did}").json()["content"] == "v007"


def test_version_settings_bound_automatic_versions(c):
    pid, did = project(c)
    _every_change(pid)
    for n in range(6):
        _edit(did, f"v{n:03d}")
        project_history.auto(pid)
    assert len([r for r in _snapshots(pid) if r["kind"] == "auto"]) == 7
    body = {"auto_minutes": 5, "auto_keep": 2}
    response = c.put(f"/api/projects/{pid}/versioning", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["auto_keep"] == 2 and not response.json()["git"]
    assert len([r for r in _snapshots(pid) if r["kind"] == "auto"]) == 3  # lowering the cap prunes at once (+ anchor)
    assert c.put(f"/api/projects/{pid}/versioning", json={**body, "auto_minutes": 10081}).status_code == 400


def test_retention_frees_snapshot_bytes(c):
    from backend.services import quota
    pid, did = project(c)
    _every_change(pid, keep=3)
    for n in range(6):
        _edit(did, f"v{n:03d}")
        project_history.auto(pid)
    before = quota.user_bytes("alice")
    _edit(did, "v999")
    project_history.auto(pid)  # every save past the cap prunes the oldest automatic version
    assert quota.user_bytes("alice") <= before


# --- #4 drift handling on proposal accept -----------------------------------

def package(c, pid, did, base, changes, title="Review"):
    text = base
    for old, new in changes:
        text = text.replace(old, new, 1)
    body = {"title": title, "files": [{
        "kind": "document", "doc_id": did, "path": "main.typ", "action": "modify",
        "base": base, "content": text,
        "changes": [{"old_string": o, "new_string": n} for o, n in changes],
    }]}
    response = c.post(f"/api/projects/{pid}/proposals", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_selected_drift_marks_stale_instead_of_409(c):
    register_user(c, "bob")
    pid, did = project(c, "alpha beta gamma\n")  # logged in as alice (owner)
    member(c, pid, "bob", "reviewer")
    login(c, "bob")
    pack = package(c, pid, did, "alpha beta gamma\n", [("beta", "BETA")])
    login(c, "alice")
    # drift: the passage disappears BEFORE the accept
    r = c.post(f"/api/docs/{did}/save", json={"content": "alpha gamma\n", "force": True})
    assert r.status_code == 200, r.text
    file = pack["files"][0]
    response = c.post(f"/api/projects/{pid}/proposals/{pack['id']}/accept",
                      json={"revision": pack["revision"], "file_id": file["id"],
                            "change_id": file["changes"][0]["id"]})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["files"][0]["stale"] is True
    assert result["files"][0]["changes"] == file["changes"]
    saved = c.get(f"/api/projects/{pid}/proposals/{pack['id']}").json()
    assert saved["files"][0]["stale"] is True
    assert c.get(f"/api/docs/{did}").json()["content"] == "alpha gamma\n"


def test_whole_package_accept_stays_hard_409_with_occurrence_hint(c):
    register_user(c, "bob")
    pid, did = project(c, "alpha beta tail\n")
    member(c, pid, "bob", "reviewer")
    login(c, "bob")
    pack = package(c, pid, did, "alpha beta tail\n", [("beta", "BETA"), ("tail", "TAIL")])
    login(c, "alice")
    # drift: the passage becomes ambiguous for a whole-package accept
    r = c.post(f"/api/docs/{did}/save", json={"content": "beta tail beta\n", "force": True})
    assert r.status_code == 200, r.text
    response = c.post(f"/api/projects/{pid}/proposals/{pack['id']}/accept",
                      json={"revision": pack["revision"]})
    assert response.status_code == 409
    assert "2 occurrences" in response.json()["detail"], response.text
    assert c.get(f"/api/docs/{did}").json()["content"] == "beta tail beta\n"


def test_draft_validation_reports_zero_occurrences(c):
    pid, did = project(c)
    response = c.post(f"/api/projects/{pid}/proposals", json={"title": "Stale draft", "files": [{
        "kind": "document", "doc_id": did, "path": "main.typ", "action": "modify",
        "base": "base", "content": "gone", "changes": [{"old_string": "vanished", "new_string": "gone"}],
    }]})
    assert response.status_code == 409
    assert "0 occurrences" in response.json()["detail"], response.text


# --- #7 project-wide unique entry names --------------------------------------

def test_file_names_unique_case_insensitively(c):
    pid, did = project(c)
    v = put_file(c, pid, "fdgbdf.txt", b"x").json()["version"]
    assert put_file(c, pid, "FDGBDF.txt", b"x").status_code == 409
    assert put_file(c, pid, "fdgbdf.txt", b"x").status_code == 409  # existing file needs its version
    assert put_file(c, pid, "fdgbdf.txt", b"y", version=v).status_code == 200
    assert put_file(c, pid, "fdgbdf.txt/inner.txt", b"y").status_code == 409  # file and folder cannot share a name


def test_file_move_collision(c):
    pid, did = project(c)
    v = put_file(c, pid, "a.csv", b"1,2").json()["version"]
    put_file(c, pid, "new.csv", b"z")
    r = c.post(f"/api/projects/{pid}/files-move", json={"path": "a.csv", "target": "b.csv", "version": v})
    assert r.status_code == 200, r.text
    # moving onto another file's name is a real collision
    assert c.post(f"/api/projects/{pid}/files-move", json={"path": "b.csv", "target": "NEW.csv", "version": v}).status_code == 409


def test_case_only_rename_of_a_file_is_allowed(c):
    pid, _did = project(c)
    v = put_file(c, pid, "module.typ", b"a").json()["version"]
    r = c.post(f"/api/projects/{pid}/files-move", json={"path": "module.typ", "target": "Module.typ", "version": v})
    assert r.status_code == 200, r.text
    assert [f["path"] for f in c.get(f"/api/projects/{pid}/files").json()["files"]] == ["Module.typ"]


def test_zip_import_respects_existing_file_names(c):
    pid, did = project(c)
    assert put_file(c, pid, "im.png", b"img").status_code == 200
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("main.typ", "x")
        archive.writestr("IM.PNG", b"other")
    response = c.post(f"/api/projects/{pid}/import-zip",
                      data={"documents": '["main.typ"]'}, files={"f": ("src.zip", data.getvalue(), "application/zip")})
    assert response.status_code == 409, response.text
    assert c.get(f"/api/projects/{pid}/files/im.png").content == b"img"


# --- #6 open review packages in the topbar summary ---------------------------

def proj_package(c, pid, title="Review", old="x", new="y", content="x"):
    did = c.get(f"/api/projects/{pid}").json()["documents"][0]["id"]
    body = {"kind": "document", "doc_id": did, "path": "main.typ", "action": "modify",
            "base": content, "content": content.replace(old, new),
            "changes": [{"old_string": old, "new_string": new}]}
    response = c.post(f"/api/projects/{pid}/proposals", json={"title": title, "files": [body]})
    assert response.status_code == 200, response.text
    return response.json()


def test_summary_lists_open_packages_of_others(c):
    register_user(c, "alice")
    register_user(c, "bob")
    login(c, "bob")
    did = make_doc(c, "Team", "x")
    pid = c.get(f"/api/docs/{did}").json()["project_id"]
    member(c, pid, "alice", "reviewer")
    authored = proj_package(c, pid)
    # the author never sees their own package on the summary
    mine = c.get("/api/notifications/summary").json()
    assert mine["packages"] == [] and mine["package_total"] == 0
    login(c, "alice")
    extra = proj_package(c, pid, title="Second")
    # reviewers see the open packages of the other author (author filter)
    summary = c.get("/api/notifications/summary").json()
    assert [p["id"] for p in summary["packages"]] == [authored["id"]]
    assert summary["package_total"] == 1
    first = summary["packages"][0]
    assert {"project", "id", "title", "author", "updated_at"} <= set(first)
    assert first["project"] == pid and first["author"] == "bob"
    # a non-member stays empty even with a session
    register_user(c, "eve")
    login(c, "eve")
    empty = c.get("/api/notifications/summary").json()
    assert empty["packages"] == [] and empty["package_total"] == 0
    # acceptance empties the package; the accepted side then drops off the summary
    login(c, "bob")
    r = c.post(f"/api/projects/{pid}/proposals/{extra['id']}/accept",
               json={"revision": extra["revision"]})
    assert r.status_code == 200, r.text
    summary = c.get("/api/notifications/summary").json()
    assert summary["packages"] == [] and summary["package_total"] == 0


def test_summary_rejects_anonymous(c):
    assert c.get("/api/notifications/summary").status_code == 401
