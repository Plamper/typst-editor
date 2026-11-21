"""Project-wide history and file actions."""
from conftest import login, put_file, register_user

from backend import db


def test_checkpoint_restores_documents_and_every_project_file(c):
    register_user(c, 'alice')
    did = c.post('/api/docs/create', json={'title': 'First', 'content': 'first source'}).json()['id']
    pid = c.get(f'/api/docs/{did}').json()['project_id']
    other = c.post(f'/api/projects/{pid}/docs', json={'title': 'Second', 'content': 'second source'}).json()['id']
    v = put_file(c, pid, 'data.csv', b'a,b\n1,2').json()['version']
    img = put_file(c, pid, 'image.png', b'\x89PNG\x00\xff').json()['version']
    snap = c.post(f'/api/projects/{pid}/snapshots', json={'label': 'Whole project'})
    assert snap.status_code == 200, snap.text
    sid = snap.json()['id']
    put_file(c, pid, 'data.csv', b'changed', version=v)
    c.delete(f'/api/projects/{pid}/files/image.png', params={'version': img})
    put_file(c, pid, 'later.txt', b'later')
    c.delete(f'/api/docs/{other}')
    added = c.post(f'/api/projects/{pid}/docs', json={'title': 'Added later'}).json()['id']
    diff = c.get(f'/api/projects/{pid}/snapshots/{sid}').json()
    assert 'Second' in diff['content'] and 'image.png' in diff['content']
    assert diff['content'] != diff['current_content']
    result = c.post(f'/api/projects/{pid}/snapshots/{sid}/restore', json={})
    assert result.status_code == 200, result.text
    assert c.get(f'/api/docs/{other}').json()['content'] == 'second source'
    assert c.get(f'/api/projects/{pid}/files/image.png').content == b'\x89PNG\x00\xff'
    assert c.get(f'/api/projects/{pid}/files/data.csv').content == b'a,b\n1,2'
    assert c.get(f'/api/projects/{pid}/files/later.txt').status_code == 404
    with db.tx() as con:
        assert con.execute('SELECT trashed FROM docs WHERE id=?', (added,)).fetchone()[0] == 1
    history = c.get(f'/api/projects/{pid}/snapshots').json()['snapshots']
    before = next(s for s in history if s['label'] == 'Before restore')
    assert before['kind'] == 'manual'  # the safety net must survive auto-retention


def test_project_file_moves_and_editor_delete(c):
    register_user(c, 'alice')
    did = c.post('/api/docs/create', json={'title': 'Rename'}).json()['id']
    pid = c.get(f'/api/docs/{did}').json()['project_id']
    v = put_file(c, pid, 'old.typ', b'original').json()['version']
    put_file(c, pid, 'occupied.typ', b'keep')
    url = f'/api/projects/{pid}/files-move'
    assert c.post(url, json={'path': 'old.typ', 'target': 'occupied.typ', 'version': v}).status_code == 409
    body = {'path': 'old.typ', 'target': 'new.typ'}
    preview = c.post(url + '-preview', json=body).json()
    assert c.post(url, json={**body, 'version': preview['version']}).status_code == 200
    put_file(c, pid, 'a.png', b'image')
    body = {'path': 'a.png', 'target': 'nested/b.png'}
    preview = c.post(url + '-preview', json=body).json()
    assert c.post(url, json={**body, 'version': preview['version']}).status_code == 200
    assert c.get(f'/api/projects/{pid}/files/nested/b.png').content == b'image'
    register_user(c, 'bob')
    login(c, 'alice')
    c.post(f'/api/projects/{pid}/members', json={'username': 'bob', 'role': 'editor'})
    login(c, 'bob')
    assert c.post(f'/api/docs/{did}/rename', json={'title': 'Renamed by editor'}).status_code == 200
    assert c.delete(f'/api/docs/{did}').status_code == 200
    assert c.delete(f'/api/docs/{did}').status_code == 403
    assert c.get(f'/api/projects/{pid}').json()['documents'] == []
    login(c, 'alice')
    c.post(f'/api/projects/{pid}/members', json={'username': 'bob', 'role': 'reviewer'})
    login(c, 'bob')
    assert c.post(url, json={'path': 'new.typ', 'target': 'bad.typ'}).status_code == 403
    assert c.post(f'/api/projects/{pid}/snapshots', json={}).status_code == 403


def test_checkpoint_rename_delete_rights_and_compare(c):
    register_user(c, 'alice')
    register_user(c, 'bob')
    login(c, 'alice')
    did = c.post('/api/docs/create', json={'title': 'Doc', 'content': 'one'}).json()['id']
    pid = c.get(f'/api/docs/{did}').json()['project_id']
    assert c.post(f'/api/projects/{pid}/members', json={'username': 'bob', 'role': 'editor'}).status_code == 200
    first = c.post(f'/api/projects/{pid}/snapshots', json={'label': ''}).json()['id']
    assert c.patch(f'/api/projects/{pid}/snapshots/{first}', json={'label': 'Draft 1'}).json() == {'label': 'Draft 1', 'kind': 'manual'}
    listed = c.get(f'/api/projects/{pid}/snapshots').json()['snapshots']
    assert ('Draft 1', 'alice') in [(s['label'], s['author']) for s in listed]
    login(c, 'bob')
    bobs = c.post(f'/api/projects/{pid}/snapshots', json={'label': 'Bob'}).json()['id']
    assert c.delete(f'/api/projects/{pid}/snapshots/{first}').status_code == 403  # alice's, bob is not owner
    assert c.patch(f'/api/projects/{pid}/snapshots/{first}', json={'label': 'Renamed by editor'}).status_code == 403
    assert c.patch(f'/api/projects/{pid}/snapshots/{bobs}', json={'label': 'Bob 2'}).json()['label'] == 'Bob 2'  # own checkpoint
    # compare two stored checkpoints and a missing one
    cmp = c.get(f'/api/projects/{pid}/snapshots/{first}', params={'against': bobs}).json()
    assert cmp['changes'] == [] and cmp['against']['id'] == bobs and cmp['against']['label'] == 'Bob 2'
    assert c.get(f'/api/projects/{pid}/snapshots/{first}', params={'against': 'nope'}).status_code == 404
    assert c.delete(f'/api/projects/{pid}/snapshots/{bobs}').status_code == 200  # own checkpoint
    login(c, 'alice')
    assert c.delete(f'/api/projects/{pid}/snapshots/{first}').status_code == 200  # owner
    assert c.delete(f'/api/projects/{pid}/snapshots/{first}').status_code == 404


def test_naming_an_automatic_checkpoint_keeps_it(c):
    register_user(c, 'alice')
    did = c.post('/api/docs/create', json={'title': 'Doc'}).json()['id']
    pid = c.get(f'/api/docs/{did}').json()['project_id']
    with db.tx() as con:
        con.execute("INSERT INTO project_snapshots (id,project_id,content,label,created_at,kind) VALUES ('ps_auto',?,'{}','','2000-01-01T00:00:00+00:00','auto')", (pid,))
    assert c.patch(f'/api/projects/{pid}/snapshots/ps_auto', json={'label': 'Release'}).json()['kind'] == 'manual'
    assert c.patch(f'/api/projects/{pid}/snapshots/ps_auto', json={'label': ''}).json()['kind'] == 'manual'


def test_changes_use_sidebar_paths_and_mark_binaries(c):
    register_user(c, 'alice')
    did = c.post('/api/docs/create', json={'title': 'Thesis', 'content': 'one'}).json()['id']
    pid = c.get(f'/api/docs/{did}').json()['project_id']
    sid = c.post(f'/api/projects/{pid}/snapshots', json={'label': 'base'}).json()['id']
    put_file(c, pid, 'lib/style.typ', b'#let a = 1')
    put_file(c, pid, 'logo.png', b'\x89PNG\x00\xff')
    rows = c.get(f'/api/projects/{pid}/snapshots/{sid}').json()['changes']
    assert [(r['name'], r['status'], r['binary']) for r in rows] == [
        ('lib/style.typ', 'added', False), ('logo.png', 'added', True)]  # no project name, no main.typ
    assert rows[1]['after_size'] == 6 and rows[1]['after'] == ''


def test_editor_cannot_name_a_foreign_automatic_checkpoint(c):
    register_user(c, 'alice')
    register_user(c, 'bob')
    login(c, 'alice')
    did = c.post('/api/docs/create', json={'title': 'Doc'}).json()['id']
    pid = c.get(f'/api/docs/{did}').json()['project_id']
    assert c.post(f'/api/projects/{pid}/members', json={'username': 'bob', 'role': 'editor'}).status_code == 200
    with db.tx() as con:
        con.execute("INSERT INTO project_snapshots (id,project_id,content,label,created_at,kind,author) "
                    "VALUES ('ps_auto_editor',?,'{}','','2000-01-01T00:00:00+00:00','auto','')", (pid,))
    login(c, 'bob')
    assert c.patch(f'/api/projects/{pid}/snapshots/ps_auto_editor', json={'label': 'Pinned'}).status_code == 403
    login(c, 'alice')
    assert c.patch(f'/api/projects/{pid}/snapshots/ps_auto_editor', json={'label': 'Pinned'}).json()['kind'] == 'manual'
