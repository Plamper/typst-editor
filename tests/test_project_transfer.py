"""Project names, copy/move ACLs and collision handling."""
import io
import zipfile

import pytest
from conftest import login, put_file, register_user
from pycrdt import Doc, Text

from backend import db, sync


def paths(c, pid):
    return [f['path'] for f in c.get(f'/api/projects/{pid}').json()['files']]


def setup_projects(c):
    register_user(c, 'alice')
    ids = [c.post('/api/docs/create', json={'title': title}).json()['id'] for title in ['A', 'B']]
    pids = [c.get(f'/api/docs/{did}').json()['project_id'] for did in ids]
    return ids, pids


def test_document_names_are_folder_local_and_restore_never_overwrites(c):
    ids, pids = setup_projects(c)
    docs = []
    for pid in pids:
        r = c.post(f'/api/projects/{pid}/docs', json={'title': 'main.typ', 'content': pid})
        assert r.status_code == 200, r.text
        docs.append(r.json()['id'])
    assert c.post(f'/api/projects/{pids[0]}/docs', json={'title': 'MAIN.typ'}).status_code == 409
    sibling = c.post(f'/api/projects/{pids[0]}/docs', json={'title': 'main.typ', 'folder': 'chapters'})
    assert sibling.status_code == 200, sibling.text  # same title, different folder
    assert c.post(f'/api/projects/{pids[0]}/docs', json={'title': 'MAIN.typ', 'folder': 'chapters'}).status_code == 409
    assert c.post(f'/api/docs/{sibling.json()["id"]}/rename', json={'title': 'A'}).status_code == 200  # rename within a folder
    assert c.delete(f'/api/docs/{docs[0]}').status_code == 200
    replacement = c.post(f'/api/projects/{pids[0]}/docs', json={'title': 'main.typ', 'content': 'keep'})
    assert replacement.status_code == 200, replacement.text
    assert c.post(f'/api/docs/{docs[0]}/restore').status_code == 409
    assert c.get(f'/api/docs/{replacement.json()["id"]}').json()['content'] == 'keep'
    assert c.post(f'/api/docs/{docs[1]}/rename', json={'title': 'A'}).status_code == 200


def test_same_project_document_copy_keeps_its_title_in_another_folder(c):
    ids, (pid, _target) = setup_projects(c)
    did = c.post(f'/api/projects/{pid}/docs', json={'title': 'Chapter', 'folder': 'old', 'content': '#image("/bilder/pfeil.svg")'}).json()['id']
    assert put_file(c, pid, 'bilder/pfeil.svg', b'<svg/>').status_code == 200
    url = f'/api/projects/{pid}/transfer'
    body = {'kind': 'document', 'doc_id': did, 'target_project_id': pid, 'target_name': 'Chapter', 'target_folder': 'new'}
    copied = c.post(url, json=body)
    assert copied.status_code == 200, copied.text
    doc = c.get(f'/api/docs/{copied.json()["doc_id"]}').json()
    assert (doc['title'], doc['folder']) == ('Chapter', 'new')
    assert paths(c, pid) == ['bilder/pfeil.svg']  # the project file stays shared
    assert c.post(url, json={**body, 'target_folder': 'old'}).status_code == 409  # same folder needs a new title
    duplicate = c.post(url, json={**body, 'target_folder': 'old', 'duplicate': True})
    assert duplicate.status_code == 200, duplicate.text
    assert c.get(f'/api/docs/{duplicate.json()["doc_id"]}').json()['title'] == 'Chapter (copy)'
    assert paths(c, pid) == ['bilder/pfeil.svg']  # no file copies


def test_document_copy_rejects_a_conflicting_file_in_the_target(c):
    ids, (source, target) = setup_projects(c)
    did = c.post(f'/api/projects/{source}/docs', json={'title': 'Doc', 'content': '#image("img.png")'}).json()['id']
    put_file(c, source, 'img.png', b'\x89PNG')
    put_file(c, target, 'Img.PNG', b'other')
    result = c.post(f'/api/projects/{source}/transfer', json={
        'kind': 'document', 'doc_id': did, 'target_project_id': target, 'target_name': 'Doc'})
    assert result.status_code == 409
    assert 'img.png' in result.text
    assert did in [d['id'] for d in c.get(f'/api/projects/{source}').json()['documents']]
    assert 'Doc' not in [d['title'] for d in c.get(f'/api/projects/{target}').json()['documents']]


def test_document_transfer_brings_reached_files_and_detects_conflicts(c):
    ids, (source, target) = setup_projects(c)
    put_file(c, source, 'layout.typ', b'snapshot')
    put_file(c, source, 'unused.typ', b'stays')
    did = c.post(f'/api/projects/{source}/docs', json={'title': 'Shared name', 'folder': 'chapters/sub', 'content': '#include "/layout.typ"\n#image("pic.png")'}).json()['id']
    put_file(c, source, 'chapters/sub/pic.png', b'\x89PNG\xff')
    body = {'kind': 'document', 'doc_id': did, 'target_project_id': target, 'target_name': 'Shared name'}
    url = f'/api/projects/{source}/transfer'
    result = c.post(url, json=body)
    assert result.status_code == 200, result.text
    copied = result.json()['doc_id']
    assert copied != did
    assert c.get(f'/api/docs/{copied}').json()['folder'] == 'chapters/sub'
    assert c.get(f'/api/projects/{target}/files/chapters/sub/pic.png').content == b'\x89PNG\xff'
    assert c.get(f'/api/projects/{target}/files/layout.typ').content == b'snapshot'
    assert 'unused.typ' not in paths(c, target)
    assert c.post(url, json={**body, 'move': True}).status_code == 409
    moved = c.post(url, json={**body, 'move': True, 'target_name': 'Moved'})
    assert moved.status_code == 200, moved.text  # identical files already there: no conflict
    assert c.get(f'/api/docs/{did}').json()['project_id'] == target
    assert c.get(f'/api/docs/{did}').json()['folder'] == 'chapters/sub'
    assert did not in [d['id'] for d in c.get(f'/api/projects/{source}').json()['documents']]
    assert 'layout.typ' in paths(c, source)  # files stay where other documents may need them


def test_document_transfer_can_choose_a_target_folder(c):
    ids, (source, target) = setup_projects(c)
    did = c.post(f'/api/projects/{source}/docs', json={'title': 'Chapter', 'folder': 'old', 'content': 'x'}).json()['id']
    url = f'/api/projects/{source}/transfer'
    body = {'kind': 'document', 'doc_id': did, 'target_project_id': target, 'target_name': 'Chapter', 'target_folder': 'inbox'}
    copied = c.post(url, json=body)
    assert copied.status_code == 200, copied.text
    assert c.get(f'/api/docs/{copied.json()["doc_id"]}').json()['folder'] == 'inbox'
    duplicate = c.post(url, json={'kind': 'document', 'doc_id': did, 'target_project_id': source, 'duplicate': True, 'target_folder': 'copies'})
    assert duplicate.status_code == 200, duplicate.text
    assert c.get(f'/api/docs/{duplicate.json()["doc_id"]}').json()['folder'] == 'copies'
    assert c.post(url, json={**body, 'target_name': 'Deep', 'target_folder': '/'.join('abcdefghijk')}).status_code == 400
    moved = c.post(url, json={**body, 'target_name': 'Moved chapter', 'move': True, 'target_folder': 'archive'})
    assert moved.status_code == 200, moved.text
    assert c.get(f'/api/docs/{did}').json()['folder'] == 'archive'


def test_file_transfer_acl_and_independence(c):
    ids, (source, target) = setup_projects(c)
    put_file(c, source, 'sub/root.typ', b'#include "leaf.typ"')
    url = f'/api/projects/{source}/transfer'
    body = {'kind': 'file', 'name': 'sub/root.typ', 'target_project_id': target, 'target_name': 'copy/root.typ'}
    result = c.post(url, json=body)
    assert result.status_code == 200, result.text
    assert c.get(f'/api/projects/{target}/files/copy/root.typ').content == b'#include "leaf.typ"'
    assert c.post(url, json=body).status_code == 409
    put_file(c, source, 'photo.png', b'bytes')
    binary = {'kind': 'file', 'name': 'photo.png', 'target_project_id': target, 'target_name': 'photo.png', 'move': True}
    assert c.post(url, json=binary).status_code == 200
    assert c.get(f'/api/projects/{source}/files/photo.png').status_code == 404
    assert c.get(f'/api/projects/{target}/files/photo.png').content == b'bytes'
    register_user(c, 'bob')
    login(c, 'alice')
    c.post(f'/api/projects/{source}/members', json={'username': 'bob', 'role': 'reviewer'})
    c.post(f'/api/projects/{target}/members', json={'username': 'bob', 'role': 'editor'})
    login(c, 'bob')
    assert c.post(url, json={**body, 'target_name': 'reader/root.typ', 'move': True}).status_code == 403
    assert c.post(url, json={**body, 'target_name': 'reader/root.typ'}).status_code == 200


def test_zip_import_into_shared_project_bills_and_scopes_to_project_owner(c):
    ids, (source, target) = setup_projects(c)
    register_user(c, 'bob')
    login(c, 'alice')
    c.post(f'/api/projects/{target}/members', json={'username': 'bob', 'role': 'editor'})
    login(c, 'bob')
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('main.typ', '#include "module.typ"')
        archive.writestr('module.typ', 'ZIP module')
    r = c.post(f'/api/projects/{target}/import-zip', data={'documents': '["main.typ"]'}, files={'f': ('source.zip', data.getvalue(), 'application/zip')})
    assert r.status_code == 200, r.text
    did = r.json()['id']
    with db.tx() as con:
        row = con.execute('SELECT owner,project_id FROM docs WHERE id=?', (did,)).fetchone()
        assert tuple(row) == ('alice', target)
    assert c.get(f'/api/projects/{target}/files/module.typ').content == b'ZIP module'


def test_direct_duplicates_choose_copy_names_without_overwriting(c):
    ids, (source, _) = setup_projects(c)
    put_file(c, source, 'layout.typ', b'original')
    put_file(c, source, 'image.png', b'image')
    for kind, name, did in [('file', 'layout.typ', ''), ('document', 'A', ids[0]), ('file', 'image.png', '')]:
        body = {'kind': kind, 'name': name, 'doc_id': did, 'target_project_id': source, 'duplicate': True}
        first = c.post(f'/api/projects/{source}/transfer', json=body)
        second = c.post(f'/api/projects/{source}/transfer', json=body)
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert '(copy)' in first.json()['name']
        assert '(copy 2)' in second.json()['name']
    assert c.get(f'/api/projects/{source}/files/image%20(copy).png').content == b'image'
    for name in ('layout (copy).typ', 'layout (copy 2).typ', 'layout.typ'):
        assert c.get(f'/api/projects/{source}/files/{name}').content == b'original'


def test_private_module_copy_name_remains_usable_in_document_references(c):
    register_user(c, 'alice')
    c.post('/api/templates', json={'name': 'module.typ', 'content': 'Copied module'})
    for expected in ['module (copy).typ', 'module (copy 2).typ']:
        result = c.post('/api/templates/module.typ/duplicate', json={})
        assert result.status_code == 200, result.text
        assert result.json()['name'] == expected
        assert c.get('/api/templates/' + expected).json()['content'] == 'Copied module'
    result = c.post('/api/docs/create', json={'title': 'Uses copy', 'content': '#include "module (copy).typ"'})
    assert result.status_code == 200, result.text
    pid = c.get(f'/api/docs/{result.json()["id"]}').json()['project_id']
    assert c.get(f'/api/projects/{pid}/files/module%20(copy).typ').content == b'Copied module'


def test_project_zip_contains_the_tree_for_readers(c):
    ids, (source, _) = setup_projects(c)
    c.post(f'/api/projects/{source}/docs', json={'title': 'Second', 'folder': 'ch', 'content': 'second'})
    put_file(c, source, 'unused.typ', b'unused')
    put_file(c, source, 'img/image.png', b'image')
    put_file(c, source, 'ch/Second.typ', b'clash')
    c.post(f'/api/docs/{ids[0]}/save', json={'content': '#image("img/image.png")', 'force': True})
    register_user(c, 'bob')
    login(c, 'alice')
    c.post(f'/api/projects/{source}/members', json={'username': 'bob', 'role': 'reviewer'})
    login(c, 'bob')
    result = c.get(f'/api/projects/{source}/export-zip')
    assert result.status_code == 200, result.text
    with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
        assert archive.read('ch/Second.typ') == b'second'
        assert archive.read('A.typ') == b'#image("img/image.png")'
        assert archive.read('img/image.png') == b'image'
        assert archive.read('unused.typ') == b'unused'
        assert 'ch/Second.typ' in archive.read('SKIPPED.txt').decode()
    login(c, 'alice')
    c.delete(f'/api/projects/{source}/members/bob')
    c.post(f'/api/docs/{ids[0]}/share', json={'username': 'bob', 'role': 'reviewer'})
    login(c, 'bob')
    result = c.get(f'/api/projects/{source}/export-zip')
    assert result.status_code == 200, result.text
    with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
        assert sorted(archive.namelist()) == ['A.typ', 'img/image.png']


@pytest.mark.parametrize('kind', ['document', 'file'])
def test_cross_owner_move_requires_source_owner_but_copy_is_allowed(c, kind):
    ids, (source, _) = setup_projects(c)
    put_file(c, source, 'photo.png', b'image')
    register_user(c, 'bob')
    target_doc = c.post('/api/docs/create', json={'title': 'Bob'}).json()['id']
    target = c.get(f'/api/docs/{target_doc}').json()['project_id']
    login(c, 'alice')
    c.post(f'/api/projects/{source}/members', json={'username': 'bob', 'role': 'editor'})
    login(c, 'bob')
    body = {'kind': kind, 'doc_id': ids[0], 'name': 'photo.png',
            'target_project_id': target, 'target_name': 'copied.typ' if kind != 'file' else 'copied.png'}
    url = f'/api/projects/{source}/transfer'
    assert c.post(url, json={**body, 'move': True}).status_code == 403
    assert c.post(url, json=body).status_code == 200
    # Owner can explicitly move the original into an editable foreign project.
    c.post(f'/api/projects/{target}/members', json={'username': 'alice', 'role': 'editor'})
    login(c, 'alice')
    result = c.post(url, json={**body, 'move': True, 'target_name': 'moved.typ' if kind != 'file' else 'moved.png'})
    assert result.status_code == 200, result.text


def test_move_persists_live_text_and_matching_yjs_before_room_restart(c):
    ids, (source, target) = setup_projects(c)
    did = ids[0]
    old = Doc()
    old.get('typst', type=Text).insert(0, 'old')
    db.save_room(did, old.get_update(), 'old')
    room = sync.room(did)
    room['doc'].get('typst', type=Text).insert(3, ' live edits')
    result = c.post(f'/api/projects/{source}/transfer', json={'kind': 'document', 'doc_id': did,
                    'target_project_id': target, 'target_name': 'Moved', 'move': True})
    assert result.status_code == 200, result.text
    assert c.get(f'/api/docs/{did}').json()['content'] == 'old live edits'
    sync.rooms.pop(did)
    assert str(sync.room(did)['doc'].get('typst', type=Text)) == 'old live edits'
