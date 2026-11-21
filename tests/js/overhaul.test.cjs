const assert = require('node:assert/strict')
const { test } = require('node:test')
const { readFileSync } = require('node:fs')
const { resolve } = require('node:path')
const vm = require('node:vm')

const root = resolve(__dirname, '../..')
function part(file, start, end) {
  const source = readFileSync(resolve(root, file), 'utf8')
  const a = source.indexOf(start), b = source.indexOf(end, a + start.length)
  assert.ok(a >= 0 && b > a, 'function boundaries must exist in ' + file)
  return source.slice(a, b)
}
function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function context(extra = {}) {
  const elements = new Map()
  const c = vm.createContext({
    window: {},
    docId: 'document-a', docRole: 'owner', activeFile: 'chapter.typ', openN: 1,
    tplName: '', tplDirty: false, tabDirty: true, tabSaveFlight: null,
    tabVersions: new Map(), fileCache: new Map(), text: 'Draft', navigator: { onLine: true },
    tabCanEdit: () => true, tabName: k => k, tabReq: key => key, lastFiles: [], TextEncoder,
    putFile: (url, body) => c.api('PUT', url, body),
    toast: () => {}, t: (key, args) => key + (args?.msg || ''), wc: () => '',
    clearTimeout, fileT: null, myTpl: [], localStorage: { removeItem: () => {} }, saveDocScroll() {},
    fileStashKey: (doc, key) => doc + key, queueFileSave: () => { c.rearmed++ }, rearmed: 0,
    $: id => { if (!elements.has(id)) elements.set(id, { style: {}, textContent: '' }); return elements.get(id) },
    ...extra,
  })
  c.getT = () => c.text
  return c
}
const saveSource = part('web/js/80-files-images.js', 'async function flushTab(', 'function tabLive()')

test('failed file save preserves the dirty buffer and blocks leaving it', async () => {
  const c = context({ api: async () => { throw new Error('conflict') } })
  vm.runInContext(saveSource, c)
  assert.equal(await c.flushTab(), false)
  assert.equal(c.tabDirty, true)
  assert.equal(c.text, 'Draft')
  assert.match(c.$('save').textContent, /conflict/)
})

test('typing during a save keeps new text dirty and advances its base version', async () => {
  const pending = deferred()
  const c = context({ api: () => pending.promise })
  vm.runInContext(saveSource, c)
  const saving = c.flushTab()
  c.text = 'Newer draft'
  pending.resolve({ version: 'new-base' })
  assert.equal(await saving, false)
  assert.equal(c.tabVersions.get('chapter.typ'), 'new-base')
  assert.equal(c.tabDirty, true)
  assert.equal(c.text, 'Newer draft')
  assert.equal(c.rearmed, 1)
})

test('simultaneous flushes serialize instead of posting the same base twice', async () => {
  const pending = deferred()
  let calls = 0
  const c = context({ api: () => { calls++; return pending.promise } })
  vm.runInContext(saveSource, c)
  const first = c.flushTab(), second = c.flushTab()
  assert.equal(calls, 1)
  pending.resolve({ version: 'saved' })
  assert.equal(await first, true)
  assert.equal(await second, true)
  assert.equal(calls, 1)
  assert.equal(c.tabDirty, false)
})

test('an old document save cannot clear a new document draft', async () => {
  const pending = deferred()
  const c = context({ api: () => pending.promise })
  vm.runInContext(saveSource, c)
  const saving = c.flushTab()
  c.docId = 'document-b'; c.text = 'Other document'; c.tabDirty = true
  pending.resolve({ version: 'old-version' })
  await saving
  assert.equal(c.tabDirty, true)
  assert.equal(c.text, 'Other document')
  assert.equal(c.tabVersions.size, 0)
})

test('a file read does not change the active buffer before it finishes', async () => {
  const pending = deferred()
  const c = context({ activeFile: '', tabDirty: false, tabSeq: 0, TEXT_EXT: /\.(typ|bib|csv|tsv|json|yaml|yml|toml|xml|txt|csl)$/i,
    lastFiles: [{ path: 'chapter.typ', version: 'v1' }], loadProjectFiles: async () => true, fileText: () => pending.promise,
    flushTab: async () => true, tabSaveState: () => {}, setT: () => { throw new Error('must not replace dirty text') },
  })
  vm.runInContext(part('web/js/80-files-images.js', 'async function switchTab(', 'function offerFileStash('), c)
  const switching = c.switchTab('chapter.typ')
  await Promise.resolve()
  assert.equal(c.activeFile, '')
  c.text = 'Typed into main while the file loads'; c.tabDirty = true
  pending.resolve('File text')
  assert.equal(await switching, false)
  assert.equal(c.activeFile, '')
  assert.equal(c.text, 'Typed into main while the file loads')
})

test('a delayed formatter does not format a different document', async () => {
  const pending = deferred()
  let formatted = false
  const c = context({ activeFile: '', fmtEditable: () => true, fmtLoad: () => pending.promise })
  vm.runInContext(part('web/js/features/format.js', 'async function formatDoc(', 'const fmtSaveOn'), c)
  const formatting = c.formatDoc(false)
  c.docId = 'document-b'; c.openN++; c.text = 'New document'
  pending.resolve({ format: () => { formatted = true; return 'WRONG' } })
  assert.equal(await formatting, false)
  assert.equal(formatted, false)
  assert.equal(c.text, 'New document')
})

test('project files are editable only with the project edit right, the document text follows the document role', () => {
  const c = context({ docRole: 'editor', openedTrashed: false, docResReadonly: true, TEXT_EXT: /\.(typ|bib|csv|tsv|json|yaml|yml|toml|xml|txt|csl)$/i })
  const source = readFileSync(resolve(root, 'web/js/80-files-images.js'), 'utf8')
  const line = source.split('\n').find(s => s.startsWith('const tabCanEdit ='))
  vm.runInContext(line + '\nthis.canEdit = tabCanEdit', c)
  assert.equal(c.canEdit('Chapters/part.typ'), false) // a guest of one shared document reads the files it reaches
  assert.equal(c.canEdit(''), true)
  c.docResReadonly = false
  assert.equal(c.canEdit('Chapters/part.typ'), true)
  assert.equal(c.canEdit('img/logo.png'), false)
  c.docRole = 'reviewer'
  assert.equal(c.canEdit('Chapters/part.typ'), false)
})

test('template saves cannot mark input typed during the request as saved', async () => {
  const pending = deferred()
  const c = context({ tplName: 'layout.typ', tplDirty: true, tplVer: 'v1', tplOpenN: 1,
    tplSaving: null, tplT: null, tplCached: () => null, resourceStashKey: n => n,
    queueTplSave: () => { c.rearmed++ }, loadTpl: () => {}, api: () => pending.promise,
  })
  vm.runInContext(part('web/js/60-docs.js', 'async function flushTpl()', 'async function loadTheirs('), c)
  const saving = c.flushTpl()
  c.text = 'New template draft'
  pending.resolve({ version: 'v2' })
  assert.equal(await saving, false)
  assert.equal(c.tplVer, 'v2')
  assert.equal(c.tplDirty, true)
  assert.equal(c.rearmed, 1)
})

test('opening a module right after a create asks the server when the list refresh lost the race', async () => {
  const calls = [], els = new Map()
  const c = context({
    myTpl: [], tplName: '', tplVer: '', tplDirty: false, tplOpenN: 0,
    flushTpl: async () => true, flushTab: async () => true,
    tplCached: name => c.myTpl.find(x => x.name === name),
    api: async (method, url) => { calls.push([method, url]); return { name: 'numbers.csv', content: 'a,b', version: 'v1' } },
    autoCloseDrawer: () => {}, leaveDoc: () => {}, hidePops: () => {}, setView: () => {},
    paintSuggestionControls: () => {}, paintOutline: () => {}, sidebar: () => {},
    cm: { state: { facet: () => true }, dispatch: () => {} },
    editableComp: { reconfigure: x => x }, editMode: x => x, gutter: () => {},
    setT: v => { c.text = v }, resourceStashKey: n => n,
    localStorage: { getItem: () => null, removeItem: () => {} },
    $: id => { if (!els.has(id)) els.set(id, { style: {}, classList: { add() {}, remove() {} } }); return els.get(id) },
  })
  vm.runInContext(part('web/js/60-docs.js', 'async function openTpl(', 'function queueTplSave('), c)
  await c.openTpl('numbers.csv')
  assert.deepEqual(calls, [['GET', '/api/templates/numbers.csv']])
  assert.equal(c.tplName, 'numbers.csv')
  assert.equal(c.$('title').textContent, 'numbers.csv')
  assert.equal(c.tplVer, 'v1')
  assert.equal(c.myTpl.length, 1)
})

test('an awaited resource list waits for the load that superseded it', async () => {
  const rows = [deferred(), deferred()], folders = [deferred(), deferred()]
  const lists = [[{ name: 'sources.bib' }], [{ name: 'sources.bib' }, { name: 'numbers.csv' }]]
  let next = -1
  const c = vm.createContext({
    user: 'alice', docId: '', docProject: '', activeFile: '', tabDirty: false,
    myTpl: [], tplFolders: [], tplLoadN: 0, tplLoadP: null, painted: 0,
    api: (_method, url) => {
      if (!url.endsWith('/tplfolders')) next++
      return (url.endsWith('/tplfolders') ? folders[next] : rows[next]).promise
    },
    paintTpls: () => { c.painted++ }, queueRender: () => {},
  })
  vm.runInContext(part('web/js/20-completions.js', 'async function loadTpl(', 'const fileRefs'), c)
  const stale = c.loadTpl()
  const fresh = c.loadTpl()
  let staleDone = false
  stale.then(() => { staleDone = true })
  rows[0].resolve({ templates: lists[0] }); folders[0].resolve({ folders: [] })
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(staleDone, false) // superseded load must not resolve against its stale list
  rows[1].resolve({ templates: lists[1] }); folders[1].resolve({ folders: [] })
  await stale
  await fresh
  assert.equal(staleDone, true)
  assert.deepEqual(c.myTpl.map(x => x.name), ['sources.bib', 'numbers.csv'])
  assert.equal(c.painted, 1)
})

test('same-length source edits produce different compiler shadow signatures', () => {
  const c = context()
  vm.runInContext(part('web/js/50-preview.js', 'const PV_HASH_MAX', 'const pvJobs') + '\nthis.signature = pvFileSig', c)
  assert.notEqual(c.signature('part.typ', { version: 'v1', bytes: Buffer.from('Body') }),
                  c.signature('part.typ', { version: 'v1', bytes: Buffer.from('#ody') }))
})

test('nested sidebar buttons keep their native keyboard action', () => {
  const c = context()
  vm.runInContext(part('web/js/70-comments-popovers.js', 'function kb(', 'let askRes'), c)
  const row = { hasAttribute: () => true }
  let opened = false, prevented = false
  c.kb(row, () => { opened = true })
  row.onkeydown({ target: {}, key: 'Enter', preventDefault: () => { prevented = true } })
  assert.equal(opened, false)
  assert.equal(prevented, false)
  row.onkeydown({ target: row, key: 'Enter', preventDefault: () => { prevented = true } })
  assert.equal(opened, true)
})

test('inserted references are relative inside the folder of the open text, else from the project root', () => {
  const c = context({ activeFile: '', docFolder: 'Chapters' })
  vm.runInContext(part('web/js/80-files-images.js', 'function refTo(', 'const cachedText'), c)
  assert.equal(c.refTo('Chapters/part.typ'), 'part.typ')
  assert.equal(c.refTo('img/logo.png'), '/img/logo.png')
  c.activeFile = 'Chapters/Sub/notes.typ'
  assert.equal(c.refTo('Chapters/Sub/x.csv'), 'x.csv')
  c.activeFile = ''; c.docFolder = ''
  assert.equal(c.refTo('img/logo.png'), 'img/logo.png')
})

test('the Typst formatter is unavailable for bibliography and CSV buffers', () => {
  const c = context({ docId: '', tplName: 'sources.bib', activeFile: '',
    cm: { state: { facet: () => true } }, EditorView: { editable: {} },
  })
  const source = readFileSync(resolve(root, 'web/js/features/format.js'), 'utf8')
  vm.runInContext(source.split('\n').find(s => s.startsWith('const fmtEditable =')) + '\nthis.canFormat = fmtEditable', c)
  assert.equal(c.canFormat(), false)
  c.tplName = 'data.csv'; assert.equal(c.canFormat(), false)
  c.tplName = 'layout.typ'; assert.equal(c.canFormat(), true)
})

test('late fallback diagnostics cannot paint a different document', async () => {
  const pending = deferred()
  let painted = false
  const c = context({ activeFile: '', renderN: 1, pvObs: null, window: { pdfjsLib: {} },
    typstReady: () => true, cmpDiags: [], errDiags: () => pending.promise,
    paintErr: () => { painted = true }, shadowAll: async () => { throw new Error('compile unavailable') },
  })
  c.pvStale = (id, doc, tpl) => id !== c.renderN || doc !== c.docId || tpl !== c.tplName
  vm.runInContext(part('web/js/50-preview.js', 'async function renderDirect(', 'let lastErrToast'), c)
  const rendering = c.renderDirect(1, {})
  await Promise.resolve()
  c.docId = 'document-b'
  pending.resolve([{ severity: 'error', message: 'Old error' }])
  await rendering
  assert.equal(painted, false)
  assert.equal(c.cmpDiags.length, 0)
})


test('offline module edits stay dirty without sending requests or showing errors', async () => {
  let requests = 0
  const c = context({ tplName: 'layout.typ', tplDirty: true, tplSaving: null, tplT: null,
    navigator: { onLine: false }, api: () => { requests++; throw Error('offline') },
  })
  vm.runInContext(part('web/js/60-docs.js', 'async function flushTpl()', 'async function loadTheirs('), c)
  assert.equal(await c.flushTpl(), false)
  assert.equal(requests, 0)
  assert.equal(c.tplDirty, true)
})

test('rights downgrade blocks dirty tab saves without discarding the draft', async () => {
  for (const role of ['editor', 'reviewer']) {
    let posts = 0
    const c = context({ docRole: role, tabCanEdit: () => false, api: () => { posts++ } })
    vm.runInContext(saveSource, c)
    assert.equal(await c.flushTab(), false)
    assert.equal(posts, 0)
    assert.equal(c.tabDirty, true)
    assert.equal(c.text, 'Draft')
    assert.equal(c.$('save').textContent, 'projects.localDraft')
    assert.equal(c.$('save').title, 'projects.readonlyDraft')
  }
})

test('file tabs save to their project path with the version they were loaded at', () => {
  const c = context({ docProject: 'project-a' })
  vm.runInContext(part('web/js/80-files-images.js', 'const fileUrl', 'function refTo(') +
    part('web/js/80-files-images.js', 'const tabReq', 'function paintTabs(') + ';this.req = tabReq', c)
  assert.equal(c.req('Chapters/part one.typ'), '/api/projects/project-a/files/Chapters/part%20one.typ')
  c.tabVersions.set('Chapters/part one.typ', 'abc')
  assert.equal(c.req('Chapters/part one.typ'), '/api/projects/project-a/files/Chapters/part%20one.typ?version=abc')
})

test('a superseded project file list is dropped, the newest one sets the list and the edit right', async () => {
  const lists = [deferred(), deferred()]
  let calls = 0
  const c = context({ docProject: 'project-a', filesDirty: true, filesAt: 0, FILES_TTL: 15000, lastMedia: '', docResReadonly: true,
    api: () => lists[calls++].promise, renderMediaChips: () => {}, queueTabs: () => {}, console })
  vm.runInContext(part('web/js/50-preview.js', 'async function loadProjectFiles(', 'async function pvFilesOnce('), c)
  const old = c.loadProjectFiles(true), fresh = c.loadProjectFiles(true)
  lists[1].resolve({ files: [{ path: 'b.png', version: 'v2' }], readonly: false })
  assert.equal(await fresh, true)
  lists[0].resolve({ files: [{ path: 'a.png', version: 'v1' }], readonly: true })
  assert.equal(await old, false)
  assert.deepEqual(c.lastFiles.map(f => f.path), ['b.png'])
  assert.equal(c.docResReadonly, false)
  assert.equal(c.filesDirty, false)
})

test('sidebar role changes disable the main editor in place and preserve drafts and Yjs', () => {
  let editable
  const ytext = { marker: 'original Yjs' }
  const c = context({ activeFile: '', docRole: 'editor', openedTrashed: false, ytext,
    cm: { dispatch: x => { editable = x.effects } }, editableComp: { reconfigure: x => x }, editMode: x => x,
    updCNewTip: () => {}, hidePops: () => {}, refreshHl: () => {}, queueTabs: () => {}, renderMediaChips: () => {}, lastFiles: [],
  })
  c.tabCanEdit = () => c.docRole !== 'reviewer'
  vm.runInContext(part('web/js/60-docs.js', 'function refreshDocRole(', 'function updNavFade('), c)
  c.refreshDocRole({ own: [], shared: [{ id: c.docId, role: 'reviewer' }] })
  assert.equal(editable, false)
  assert.equal(c.docRole, 'reviewer')
  assert.equal(c.text, 'Draft')
  assert.equal(c.tabDirty, true)
  assert.equal(c.ytext, ytext)
  assert.equal(c.$('histBtn').style.display, '')
  c.refreshDocRole({ own: [{ id: c.docId }], shared: [{ id: c.docId, role: 'reviewer' }] })
  assert.equal(c.docRole, 'owner')
  assert.equal(editable, true)
})


test('readonly navigation keeps latest draft locally only with consent and working storage', async () => {
  for (const choice of ['leave', 'stay', 'storage-fails']) {
    const saved = new Map()
    let posts = 0, prompts = 0
    const c = context({ tabCanEdit: () => false, api: () => { posts++ },
      askPick: async () => { prompts++; c.text = 'Latest draft'; return choice === 'stay' ? 1 : 0 },
      localStorage: { setItem: (k, v) => { if (choice === 'storage-fails') throw Error('full'); saved.set(k, v) }, getItem: k => saved.get(k) },
    })
    c.tabVersions.set('chapter.typ', 'base-version')
    vm.runInContext(saveSource, c)
    assert.equal(await c.flushTab(), false)
    assert.equal(prompts, 0)
    assert.equal(await c.flushTab(true), choice === 'leave')
    assert.equal(posts, 0)
    assert.equal(c.tabDirty, choice !== 'leave')
    if (choice === 'leave') {
      const draft = JSON.parse(saved.get('document-achapter.typ'))
      assert.equal(draft.c, 'Latest draft')
      assert.equal(draft.version, 'base-version')
      assert.ok(Date.now() - draft.t < 1000)
    }
  }
})


test('file draft recovery retains the local copy until a successful save', () => {
  for (const text of ['Draft', 'Server text']) {
    const saved = new Map([['document-achapter.typ', JSON.stringify({ t: Date.now(), c: 'Draft', version: 'v1' })]])
    const c = context({ text, localStorage: { getItem: k => saved.get(k), removeItem: k => saved.delete(k) } })
    vm.runInContext(part('web/js/80-files-images.js', 'function offerFileStash(', 'async function flushTab('), c)
    c.offerFileStash('document-a', 'chapter.typ')
    assert.ok(saved.has('document-achapter.typ'))
  }
})

test('file draft recovery expires old copies and tolerates unavailable storage', () => {
  let removed = false, offered = false
  const c = context({ toast: () => { offered = true }, localStorage: {
    getItem: () => JSON.stringify({ t: Date.now() - 86400001, c: 'Old draft' }), removeItem: () => { removed = true },
  } })
  vm.runInContext(part('web/js/80-files-images.js', 'function offerFileStash(', 'async function flushTab('), c)
  c.offerFileStash('document-a', 'chapter.typ')
  assert.equal(removed, true)
  assert.equal(offered, false)
  c.localStorage.getItem = () => { throw Error('unavailable') }
  assert.doesNotThrow(() => c.offerFileStash('document-a', 'chapter.typ'))
})
