"""Version settings, the Git mirror of project versions and the account's Git email."""
import shutil
import subprocess

import pytest
from conftest import login, put_file, register_user

from backend import db
from backend.services import project_git, project_history

needs_git = pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")


def _project(c):
    register_user(c, "alice")
    register_user(c, "bob")
    login(c, "alice")
    did = c.post("/api/docs/create", json={"title": "Main", "content": "one"}).json()["id"]
    pid = c.get(f"/api/docs/{did}").json()["project_id"]
    assert c.post(f"/api/projects/{pid}/members", json={"username": "bob", "role": "editor"}).status_code == 200
    return pid, did


def _git(pid, *args):
    return subprocess.run(["git", *args], cwd=project_git.repo_dir(pid), capture_output=True, text=True, check=True).stdout


def _settings(**extra):
    return {"auto_minutes": 30, "auto_keep": 15, **extra}


def test_git_email_is_validated_and_kept_on_rename(c):
    register_user(c, "alice")
    login(c, "alice")
    assert c.get("/api/me").json()["git_email"] == ""
    assert c.post("/api/me/git-email", json={"git_email": "not an address"}).status_code == 400
    assert c.post("/api/me/git-email", json={"git_email": "alice@example.org"}).json() == {"git_email": "alice@example.org"}
    assert c.post("/api/me/name", json={"name": "alicia", "password": "pass1234"}).status_code == 200
    assert c.get("/api/me").json()["git_email"] == "alice@example.org"
    assert c.post("/api/me/git-email", json={"git_email": ""}).json() == {"git_email": ""}


def test_versions_store_what_changed_and_who_wrote(c):
    pid, did = _project(c)
    login(c, "bob")
    assert c.post(f"/api/docs/{did}/save", json={"content": "one\ntwo"}).status_code == 200
    put_file(c, pid, "refs.bib", b"@book{x}")
    login(c, "alice")
    assert c.post(f"/api/projects/{pid}/snapshots", json={}).status_code == 200
    newest = c.get(f"/api/projects/{pid}/snapshots").json()["snapshots"][0]
    assert (newest["summary"], newest["author"]) == ("Main.typ, refs.bib", "alice")
    assert newest["editors"] == "bob"  # saving the version is no change: alice is its author, not an editor


def test_version_settings_are_for_the_owner_and_hide_the_token(c, monkeypatch):
    pid, _did = _project(c)
    monkeypatch.setattr(project_git, "check_remote", lambda url: url.strip())
    body = _settings(remote="https://example.org/repo.git", token="secret-token", push=True)
    saved = c.put(f"/api/projects/{pid}/versioning", json=body).json()
    assert saved["has_token"] and saved["push"] and "token" not in saved
    assert "secret-token" not in c.get(f"/api/projects/{pid}/versioning").text
    assert "secret-token" not in c.get(f"/api/projects/{pid}").text
    with db.tx() as con:
        stored = con.execute("SELECT token FROM project_versioning WHERE project_id=?", (pid,)).fetchone()[0]
    assert stored and "secret-token" not in stored and project_git.unseal(stored) == "secret-token"  # encrypted at rest
    assert (project_git.repo_dir(pid).parent.parent / ".secret_key").stat().st_mode & 0o077 == 0
    kept = c.put(f"/api/projects/{pid}/versioning", json=_settings(remote="https://example.org/repo.git")).json()
    assert kept["has_token"] and not kept["push"]  # no token in the body keeps the stored one
    assert not c.put(f"/api/projects/{pid}/versioning", json=_settings(token="")).json()["has_token"]
    assert c.put(f"/api/projects/{pid}/versioning", json=_settings(branch="../main")).status_code == 400
    login(c, "bob")
    assert c.get(f"/api/projects/{pid}/versioning").status_code == 403
    assert c.put(f"/api/projects/{pid}/versioning", json=_settings()).status_code == 403


def test_remote_must_be_public_https(c):
    for url in ("http://example.org/r.git", "https://user:pw@example.org/r.git", "file:///tmp/r", "ext::sh -c id", "https://127.0.0.1/r.git"):
        with pytest.raises(Exception) as error:
            project_git.check_remote(url)
        assert getattr(error.value, "status_code", None) == 400, url


def test_tree_lays_out_documents_next_to_files():
    state = {"documents": [{"id": "d_1", "title": "Main", "folder": "", "content": "a"},
                           {"id": "d_2", "title": "notes", "folder": "ch", "content": "b"},
                           {"id": "d_3", "title": "../x", "folder": "", "content": "c"}],
             "files": {"Main.typ": "eA==", "ch/img.png": "AA=="}}
    assert project_git.tree(state) == {"Main.typ": b"x", "ch/img.png": b"\x00", "Main (d_1).typ": b"a", "ch/notes.typ": b"b", "_x.typ": b"c"}


@needs_git
def test_versions_become_commits_with_coauthors(c):
    pid, did = _project(c)
    login(c, "bob")
    assert c.post("/api/me/git-email", json={"git_email": "bob@example.org"}).status_code == 200
    login(c, "alice")
    assert c.put(f"/api/projects/{pid}/versioning", json=_settings(git=True)).status_code == 200
    project_git.drain()
    assert _git(pid, "log", "--format=%s").splitlines() == ["Automatic version"]  # starts at the newest version
    assert _git(pid, "show", "HEAD:Main.typ") == "one"
    login(c, "bob")
    assert c.post(f"/api/docs/{did}/save", json={"content": "one\ntwo"}).status_code == 200
    login(c, "alice")
    assert c.post(f"/api/projects/{pid}/snapshots", json={"label": "Draft 1"}).status_code == 200
    project_git.drain()
    body = _git(pid, "log", "-1", "--format=%B")
    assert body.startswith("Draft 1\n\nChanged: Main.typ\n")
    assert "Co-authored-by: bob <bob@example.org>" in body and "alice" not in body  # the owner commits, never co-authors
    assert _git(pid, "log", "-1", "--format=%an <%ae>").strip() == "alice <noreply@typst-editor.invalid>"  # no Git email yet
    assert c.post("/api/me/git-email", json={"git_email": "alice@example.org"}).status_code == 200
    login(c, "bob")
    assert c.post(f"/api/docs/{did}/save", json={"content": "one\ntwo\nthree"}).status_code == 200
    login(c, "alice")
    assert c.post(f"/api/projects/{pid}/snapshots", json={"label": "Draft 2"}).status_code == 200
    project_git.drain()
    assert _git(pid, "log", "-1", "--format=%an <%ae>|%cn").strip() == "alice <alice@example.org>|alice"
    listed = c.get(f"/api/projects/{pid}/snapshots").json()
    assert listed["git"] and not listed["remote"]
    assert listed["snapshots"][0]["commit_hash"] == _git(pid, "rev-parse", "HEAD").strip()
    assert _git(pid, "show", "HEAD:Main.typ") == "one\ntwo\nthree"
    with db.tx() as con:
        con.execute("UPDATE project_versioning SET auto_minutes=0 WHERE project_id=?", (pid,))
    project_history.auto(pid)  # nothing changed: no version, no commit
    project_git.drain()
    assert len(_git(pid, "log", "--format=%s").splitlines()) == 3


@needs_git
def test_push_to_remote(c, tmp_path, monkeypatch):
    pid, _did = _project(c)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    monkeypatch.setattr(project_git, "PROTOCOLS", "file")
    monkeypatch.setattr(project_git, "check_remote", lambda url: url.strip())
    saved = c.put(f"/api/projects/{pid}/versioning", json=_settings(git=True, remote=remote.as_uri(), branch="drafts", push=True)).json()
    assert saved["git"] and saved["push"]
    project_git.drain()  # turning Git on with automatic push commits and pushes the newest version
    status = c.get(f"/api/projects/{pid}/versioning").json()
    assert status["push_status"] == "" and status["pushed_at"]
    log = subprocess.run(["git", "log", "--format=%s", "drafts"], cwd=remote, capture_output=True, text=True, check=True).stdout
    assert log.splitlines() == ["Automatic version"]
    with db.tx() as con:
        con.execute("UPDATE project_versioning SET remote='file:///nowhere/repo.git' WHERE project_id=?", (pid,))
    sid = c.get(f"/api/projects/{pid}/snapshots").json()["snapshots"][0]["id"]
    assert c.post(f"/api/projects/{pid}/snapshots/{sid}/push").status_code == 409
    failed = c.get(f"/api/projects/{pid}/versioning").json()
    assert failed["push_status"] and failed["pushed_at"] == status["pushed_at"]


def test_unreadable_token_asks_to_enter_it_again(c, monkeypatch, tmp_path):
    monkeypatch.setattr(project_git, "check_remote", lambda url: url.strip())
    assert project_git.unseal("not-a-sealed-token") == ""
    reason = project_git._push(tmp_path, {"token": "not-a-sealed-token", "remote": "https://example.org/r.git", "branch": "main"})
    assert reason == "The saved token cannot be read; enter it again"


def test_push_errors_become_one_readable_line():
    rejected = " ! [rejected]        HEAD -> main (fetch first)\nerror: failed to push some refs\nhint: Updates were rejected"
    assert project_git._reason(rejected, "") == "The remote branch has commits this project does not have; pick another branch or reset it"
    assert project_git._reason("fatal: Authentication failed for 'https://x/'", "") == "Access denied: check the token and its push rights"
    assert project_git._reason("remote: Repository not found.\nfatal: repository 'https://x/' not found", "") == "Repository not found: check the URL and the token's access"
    assert project_git._reason("fatal: unable to access 'https://x/': tok-123 broke\nhint: retry", "tok-123") == "unable to access 'https://x/': *** broke"


@needs_git
def test_named_version_without_changes_still_gets_a_commit(c):
    pid, _did = _project(c)
    assert c.put(f"/api/projects/{pid}/versioning", json=_settings(git=True)).status_code == 200
    project_git.drain()
    assert c.post(f"/api/projects/{pid}/snapshots", json={"label": "Release"}).status_code == 200
    assert c.post(f"/api/projects/{pid}/snapshots", json={}).status_code == 200  # unnamed and unchanged: no commit
    project_git.drain()
    assert _git(pid, "log", "--format=%s").splitlines() == ["Release", "Automatic version"]


@needs_git
def test_commit_and_push_single_versions(c, tmp_path, monkeypatch):
    pid, did = _project(c)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    monkeypatch.setattr(project_git, "PROTOCOLS", "file")
    monkeypatch.setattr(project_git, "check_remote", lambda url: url.strip())
    old = c.post(f"/api/projects/{pid}/snapshots", json={"label": "Before Git"}).json()["id"]
    assert c.post(f"/api/projects/{pid}/snapshots/{old}/commit").status_code == 400  # Git is off
    assert c.post(f"/api/docs/{did}/save", json={"content": "two"}).status_code == 200
    assert c.post(f"/api/projects/{pid}/snapshots", json={"label": "Now"}).status_code == 200
    assert c.put(f"/api/projects/{pid}/versioning", json=_settings(git=True, remote=remote.as_uri(), branch="main")).status_code == 200
    project_git.drain()
    rows = {s["label"]: s for s in c.get(f"/api/projects/{pid}/snapshots").json()["snapshots"]}
    assert rows["Now"]["commit_hash"] and not rows["Now"]["pushed"] and not rows["Before Git"]["commit_hash"]
    assert c.post(f"/api/projects/{pid}/snapshots/{rows['Now']['id']}/push").json() == {"ok": True}
    rows = {s["label"]: s for s in c.get(f"/api/projects/{pid}/snapshots").json()["snapshots"]}
    assert rows["Now"]["pushed"]
    assert c.post(f"/api/projects/{pid}/snapshots/{old}/push").status_code == 409  # no commit yet
    assert c.post(f"/api/projects/{pid}/snapshots/{old}/commit").json() == {"ok": True}
    assert _git(pid, "log", "-1", "--format=%s").strip() == "Before Git" and _git(pid, "show", "HEAD:Main.typ") == "one"
    login(c, "bob")
    assert c.post(f"/api/projects/{pid}/snapshots/{old}/push").status_code == 403


def test_automatic_versions_can_be_turned_off(c):
    pid, did = _project(c)
    assert c.put(f"/api/projects/{pid}/versioning", json=_settings(auto=False, auto_minutes=0)).json()["auto"] is False
    assert c.post(f"/api/docs/{did}/save", json={"content": "changed"}).status_code == 200
    project_history.auto(pid)
    assert len(c.get(f"/api/projects/{pid}/snapshots").json()["snapshots"]) == 1  # only the creation anchor
    assert c.put(f"/api/projects/{pid}/versioning", json=_settings(auto_minutes=0)).json()["auto"] is True
    project_history.auto(pid)
    assert len(c.get(f"/api/projects/{pid}/snapshots").json()["snapshots"]) == 2
