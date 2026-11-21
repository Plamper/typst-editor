"""Project regression cases that gate the first project release candidate."""
import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import login, make_doc, put_file, register_user
from fastapi import HTTPException

from backend import db, ratelimit, sync
from backend.services import project_history, quota


def project(c):
    register_user(c, "alice")
    did = make_doc(c, "First", '#include "layout.typ"')
    return c.get(f"/api/docs/{did}").json()["project_id"], did


def test_concurrent_document_names_are_project_scoped(c):
    pid, _ = project(c)
    other = c.post('/api/projects', json={'title': 'Other'}).json()['id']
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda p: c.post(f'/api/projects/{p}/docs', json={'title': 'Same'}).status_code, [pid, pid]))
    assert sorted(results) == [200, 409]
    assert c.post(f'/api/projects/{other}/docs', json={'title': 'Same'}).status_code == 200
    with db.tx() as con:
        assert not con.execute('SELECT 1 FROM docs WHERE project_id IS NULL').fetchone()


def test_document_guest_cannot_list_project_roster(c):
    pid, did = project(c)
    register_user(c, 'member')
    register_user(c, 'guest')
    login(c, 'alice')
    c.post(f'/api/projects/{pid}/members', json={'username': 'member', 'role': 'editor'})
    c.post(f'/api/docs/{did}/share', json={'username': 'guest', 'role': 'reviewer'})
    login(c, 'guest')
    assert c.get(f'/api/docs/{did}/members').json()['members'] == ['alice', 'guest']
    login(c, 'member')
    assert 'member' in c.get(f'/api/docs/{did}/members').json()['members']


@pytest.mark.parametrize('action,bucket', [('create', 'create'), ('save', 'save'), ('upload', 'files'), ('delete', 'files')])
def test_project_writes_enforce_rate_limits(c, monkeypatch, action, bucket):
    pid, _ = project(c)
    put_file(c, pid, 'a.typ', b'a')
    real = ratelimit.allow
    monkeypatch.setattr(ratelimit, 'allow', lambda key, *args, **kwargs: False if key.startswith(bucket + ':') else real(key, *args, **kwargs))
    if action == 'create':
        response = c.post(f'/api/projects/{pid}/docs', json={'title': 'Limited'})
    elif action == 'save':
        response = put_file(c, pid, 'b.typ', b'b')
    elif action == 'upload':
        response = put_file(c, pid, 'b.png', b'b')
    else:
        response = c.delete(f'/api/projects/{pid}/files/a.typ?version=stale')
    assert response.status_code == 429, response.text


def test_move_preview_and_stale_version(c):
    from backend.services import project_files

    pid, did = project(c)
    put_file(c, pid, 'layout.typ', b'before')
    url = f'/api/projects/{pid}/files-move'
    body = {'path': 'layout.typ', 'target': 'renamed.typ'}
    preview = c.post(url + '-preview', json=body).json()
    assert preview['references'] == ['First: layout.typ']
    put_file(c, pid, 'layout.typ', b'new', version=preview['version'])
    assert c.post(url, json={**body, 'version': preview['version']}).status_code == 409
    preview = c.post(url + '-preview', json=body).json()
    assert c.post(url, json={**body, 'version': preview['version']}).status_code == 200
    assert c.get(f'/api/docs/{did}').json()['content'] == '#include "renamed.typ"'
    put_file(c, pid, 'a.png', b'picture')
    body = {'path': 'a.png', 'target': 'b.png'}
    preview = c.post(url + '-preview', json=body).json()
    assert preview['version'] == project_files.version(b'picture')
    assert c.post(url, json={**body, 'version': preview['version'], 'fix_refs': False}).status_code == 200


def test_checkpoint_capture_does_not_reserve_sqlite_writer(c, monkeypatch):
    pid, _ = project(c)
    capture = project_history.capture

    def checked(con, project_id):
        with db.tx() as writer:
            writer.execute("UPDATE users SET avatar=avatar WHERE name='alice'")
        return capture(con, project_id)

    monkeypatch.setattr(project_history, 'capture', checked)
    response = c.post(f'/api/projects/{pid}/snapshots', json={})
    assert response.status_code == 200, response.text
    sid = response.json()['id']
    assert c.get(f'/api/projects/{pid}/snapshots/{sid}').status_code == 200


def test_checkpoint_rejects_changes_during_capture(c, monkeypatch):
    pid, did = project(c)
    capture = project_history.capture

    def changed(con, project_id):
        state = capture(con, project_id)
        with db.tx() as writer:
            writer.execute("UPDATE docs SET content='new' WHERE id=?", (did,))
        return state

    monkeypatch.setattr(project_history, 'capture', changed)
    assert c.post(f'/api/projects/{pid}/snapshots', json={}).status_code == 409


def test_failed_restore_retains_sources_files_and_history(c, monkeypatch):
    pid, did = project(c)
    sid = c.post(f'/api/projects/{pid}/snapshots', json={}).json()['id']
    put_file(c, pid, 'a.png', b'keep')
    before = quota.user_bytes('alice')
    history = c.get(f'/api/projects/{pid}/snapshots').json()
    monkeypatch.setattr(quota, 'quota_cap', lambda: before)
    assert c.post(f'/api/projects/{pid}/snapshots/{sid}/restore', json={}).status_code == 413
    assert c.get(f'/api/projects/{pid}/files/a.png').content == b'keep'
    assert c.get(f'/api/projects/{pid}/snapshots').json() == history


def test_migration_allowance_and_case_collisions(c):
    pid, _ = project(c)
    with db.tx() as con:
        con.execute("CREATE TABLE project_resources (project_id TEXT NOT NULL, name TEXT NOT NULL, content TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)")
        con.execute("INSERT INTO project_resources VALUES (?,?,?,?)", (pid, 'a.typ', 'copied', db.now_iso()))
        con.execute('DROP INDEX idx_projects_owner_title_nocase')
        con.execute('ALTER TABLE users DROP COLUMN migration_bytes')
        con.execute("INSERT INTO projects VALUES ('legacy','alice','first','now',0)")
        db._migrate_v19(con)
        assert con.execute("SELECT migration_bytes FROM users WHERE name='alice'").fetchone()[0] == 6
        names = [r[0] for r in con.execute("SELECT title FROM projects WHERE owner='alice'")]
        assert len({n.lower() for n in names}) == len(names)
        con.execute('DROP TABLE project_resources')
    info = c.get('/api/me').json()['storage']
    assert info['limit'] == quota.quota_cap() + 6
    assert info['migration_allowance'] == 6


def test_mcp_shared_listing_uses_project_role_without_per_doc_queries(c, monkeypatch):
    pid, did = project(c)
    register_user(c, 'bob')
    login(c, 'alice')
    c.post(f'/api/projects/{pid}/members', json={'username': 'bob', 'role': 'reviewer'})
    assert c.put(f'/api/projects/{pid}/files/img/a.png', content=b'x').status_code == 200
    monkeypatch.setattr(db, 'role_on', lambda *args: pytest.fail('per-document role query'))
    from backend import mcp_fs
    ctx = mcp_fs.Ctx('bob', 'editor')
    assert mcp_fs.ls(ctx, '/')['entries'][0] == {'path': '/First@alice', 'kind': 'project', 'role': 'reviewer'}
    assert [e['path'] for e in mcp_fs.ls(ctx, '/First@alice')['entries'] if e['kind'] == 'doc']
    assert '/First@alice/img/a.png' in {e['path'] for e in mcp_fs.ls(ctx, '/First@alice', depth=0)['entries']}


def test_hard_delete_invalidates_quota_cache(c):
    _, did = project(c)
    c.delete(f'/api/docs/{did}')
    sync._quota_cache['alice'] = (True, 0)
    assert c.delete(f'/api/docs/{did}').status_code == 200
    assert 'alice' not in sync._quota_cache


def test_legacy_snapshot_delete_invalidates_quota_cache(c):
    _, did = project(c)
    sid = c.post(f'/api/docs/{did}/snapshots', json={}).json()['id']
    sync._quota_cache['alice'] = (True, 0)
    assert c.delete(f'/api/docs/{did}/snapshots/{sid}').status_code == 200
    assert 'alice' not in sync._quota_cache


def test_live_save_checks_final_quota_in_transaction(c, monkeypatch):
    _, did = project(c)
    before = c.get(f'/api/docs/{did}').json()['content']
    used = quota.user_bytes('alice')
    monkeypatch.setattr(quota, 'quota_cap', lambda: used)
    with pytest.raises(HTTPException) as error:
        db.save_room(did, b'new yjs', before + 'more')
    assert error.value.status_code == 413
    assert c.get(f'/api/docs/{did}').json()['content'] == before


def test_edit_arriving_during_flush_remains_dirty(c, monkeypatch):
    from pycrdt import Text

    _, did = project(c)
    room = sync.room(did)
    room['dirty'] = True
    save = db.save_room

    def concurrent(doc_id, yjs, text):
        save(doc_id, yjs, text)
        with sync._doc_lock(room), room['doc'].transaction():
            room['doc'].get('typst', type=Text).__iadd__('later')

    monkeypatch.setattr(db, 'save_room', concurrent)
    assert asyncio.run(sync._flush_one(did, room))
    assert room['dirty']
    assert sync.room_text(did).endswith('later')


def test_unicode_limits_measure_utf8_bytes(c):
    pid, did = project(c)
    text = '\U0001f600' * 50001
    assert c.post(f'/api/projects/{pid}/docs', json={'title': 'Large', 'content': text}).status_code == 400
    assert c.post(f'/api/docs/{did}/save', json={'content': text}).status_code == 400
    assert put_file(c, pid, 'large.typ', text.encode()).status_code == 413


def test_legacy_duplicate_copies_dependencies_without_mutating_source(c):
    _, did = project(c)
    c.post('/api/templates', json={'name': 'layout.typ', 'content': 'keep dependency'})
    with db.tx() as con:
        con.execute('UPDATE docs SET project_id=NULL WHERE id=?', (did,))
    response = c.post(f'/api/docs/{did}/duplicate')
    assert response.status_code == 200, response.text
    with db.tx() as con:
        assert con.execute('SELECT project_id FROM docs WHERE id=?', (did,)).fetchone()[0] is None
    copied = c.get(f"/api/docs/{response.json()['id']}").json()['project_id']
    assert c.get(f'/api/projects/{copied}/files/layout.typ').content == b'keep dependency'


def test_project_suffix_keeps_title_length_limit(c):
    register_user(c, 'alice')
    title = 'A' * 100
    first = c.post('/api/projects', json={'title': title}).json()['id']
    second = c.post('/api/projects', json={'title': title}).json()['id']
    assert c.get(f'/api/projects/{first}').json()['title'] == title
    assert c.get(f'/api/projects/{second}').json()['title'] == 'A' * 96 + ' (2)'


def test_failed_legacy_duplicate_removes_new_project(c, monkeypatch):
    _, did = project(c)
    c.post('/api/templates', json={'name': 'layout.typ', 'content': 'dependency'})
    with db.tx() as con:
        con.execute('UPDATE docs SET project_id=NULL WHERE id=?', (did,))
    before = quota.user_bytes('alice')
    projects = {p['id'] for p in c.get('/api/projects').json()['projects']}
    monkeypatch.setattr(quota, 'quota_cap', lambda: before)
    result = c.post(f'/api/docs/{did}/duplicate')
    assert result.status_code == 413, result.text
    assert quota.user_bytes('alice') == before
    assert {p['id'] for p in c.get('/api/projects').json()['projects']} == projects


def test_move_preview_of_a_vanished_file_is_404(c):
    pid, did = project(c)
    assert c.post(f'/api/projects/{pid}/files-move-preview', json={'path': 'layout.typ', 'target': 'new.typ'}).status_code == 404
