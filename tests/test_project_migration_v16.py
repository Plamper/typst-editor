from conftest import login, put_file, register_user

from backend import db


def test_creation_snapshots_private_and_guest_drawer(c):
    register_user(c, 'alice')
    c.post('/api/templates', json={'name': 'used.typ', 'content': 'snapshot'})
    c.post('/api/templates', json={'name': 'secret.typ', 'content': 'secret'})
    did = c.post('/api/docs/create', json={'title': 'First', 'content': '#include "used.typ"'}).json()['id']
    p = next(p for p in c.get('/api/projects').json()['projects'] if p['title'] == 'First')
    assert p['documents'][0]['id'] == did
    assert [f['path'] for f in p['files']] == ['used.typ']
    pid = p['id']
    c.post(f'/api/projects/{pid}/docs', json={'title': 'Other'})
    put_file(c, pid, 'other.typ', b'private project')
    register_user(c, 'bob')
    login(c, 'alice')
    c.post(f'/api/docs/{did}/share', json={'username': 'bob', 'role': 'reviewer'})
    login(c, 'bob')
    p = c.get(f'/api/projects/{pid}').json()
    assert p['role'] == 'guest'
    assert not p['can_edit_library']
    assert [d['id'] for d in p['documents']] == [did]
    assert [f['path'] for f in p['files']] == ['used.typ']
    login(c, 'alice')
    c.post(f'/api/docs/{did}/share', json={'username': 'bob', 'role': 'editor'})
    login(c, 'bob')
    result = c.post(f'/api/docs/{did}/duplicate')
    assert result.status_code == 200, result.text
    copied = c.get('/api/projects').json()['projects']
    owned = [p for p in copied if p['role'] == 'owner']
    copied = next(p for p in owned if p['title'] != 'Tutorial')
    assert [f['path'] for f in copied['files']] == ['used.typ']


def test_v16_migration_keeps_ids_and_snapshots_templates(c, monkeypatch):
    register_user(c, 'alice')
    from backend.services.docfiles import store
    with db.tx() as con:
        con.execute("INSERT INTO templates(owner,name,content,updated_at) VALUES ('alice','used.typ','shelf',?)", (db.now_iso(),))
        for did, folder, trash in [('legacy1', 'Work/sub', 0), ('legacy2', 'Work', 1), ('legacy3', '', 1)]:
            con.execute('INSERT INTO docs(id,owner,title,content,folder,trashed,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)',
                        (did, 'alice', did, '#include "used.typ"', folder, trash, db.now_iso(), db.now_iso()))
        con.execute("INSERT INTO folders VALUES ('alice','doc','Empty',?)", (db.now_iso(),))
    store.put('legacy1', 'used.typ', b'local')
    from backend.services import quota
    monkeypatch.setattr(quota, 'check_quota', lambda *args: (_ for _ in ()).throw(AssertionError('migration quota gate')))
    with db.tx() as con:
        db._migrate_v16(con)
        rows = {r['id']: dict(r) for r in con.execute('SELECT * FROM docs')}
        assert rows['legacy1']['project_id'] == rows['legacy2']['project_id']
        assert rows['legacy1']['folder'] == 'sub'
        assert con.execute('SELECT archived FROM projects WHERE id=?', (rows['legacy1']['project_id'],)).fetchone()[0] == 0
        assert con.execute('SELECT archived FROM projects WHERE id=?', (rows['legacy3']['project_id'],)).fetchone()[0] == 1
        assert con.execute("SELECT 1 FROM projects WHERE title='Empty'").fetchone()
        assert not con.execute('SELECT 1 FROM docs WHERE project_id IS NULL').fetchone()
        # The template lands where the document in folder sub reaches it; the old attachment
        # stays put until v34 moves it (it then gets sub/used-2.typ and the reference follows).
        assert con.execute("SELECT path FROM project_files WHERE project_id=? ORDER BY path", (rows['legacy1']['project_id'],)).fetchall()[0][0] == 'sub/used.typ'
    assert store.get(rows['legacy1']['project_id'], 'sub/used.typ') == b'shelf'
    assert store.get('legacy1', 'used.typ') == b'local'


def test_project_members_can_read_each_others_avatar(c):
    import base64

    register_user(c, 'alice')
    pid = c.post('/api/projects', json={'title': 'Team'}).json()['id']
    c.post(f'/api/projects/{pid}/docs', json={'title': 'Report'})
    register_user(c, 'bob')
    login(c, 'alice')
    c.post(f'/api/projects/{pid}/members', json={'username': 'bob', 'role': 'reviewer'})
    with db.tx() as con:
        con.execute('UPDATE users SET avatar=? WHERE name=?', ('png:' + base64.b64encode(b'avatar').decode(), 'alice'))
    login(c, 'bob')
    assert c.get('/api/avatar/alice').status_code == 200
    login(c, 'alice')
    c.delete(f'/api/projects/{pid}/members/bob')
    login(c, 'bob')
    assert c.get('/api/avatar/alice').status_code == 404


def test_attach_to_existing_project_keeps_independent_module_snapshot(c):
    from backend.services import projects
    from backend.services.docfiles import store

    register_user(c, 'alice')
    c.post('/api/templates', json={'name': 'layout.typ', 'content': 'Original'})
    did = c.post('/api/docs/create', json={'title': 'First', 'content': '#include "layout.typ"'}).json()['id']
    pid = c.get(f'/api/docs/{did}').json()['project_id']
    c.post('/api/templates', json={'name': 'layout.typ', 'content': 'Changed private original'})
    added = db.new_id("d_")
    with db.tx() as con:
        now = db.now_iso()
        con.execute('INSERT INTO docs(id,owner,title,content,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                    (added, 'alice', 'Second', '#include "layout.typ"', now, now))
        projects.attach_doc(con, added, project_id=pid)
    assert store.get(pid, 'layout.typ') == b'Original'
    assert [f['path'] for f in c.get(f'/api/docs/{added}/resources').json()['files']] == ['layout.typ']
