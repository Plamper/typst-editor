const assert = require('node:assert/strict')
const { test } = require('node:test')
const { readFileSync } = require('node:fs')
const vm = require('node:vm')
const history = readFileSync('web/js/84-history.js', 'utf8')

function setup() {
  const c = vm.createContext({ svgI: d => d, t: (key, p) => key + (p ? JSON.stringify(p) : ''), document: { addEventListener() {} } })
  vm.runInContext(history.slice(history.indexOf('const ICO_SLIDERS'), history.indexOf('const ICO_EYE')), c)
  return c
}

test('unnamed versions are named by their time', () => {
  const c = setup()
  const at = '2026-11-26T13:05:00+00:00', time = c.versionTime(at)
  assert.match(time, /\d/)
  assert.equal(c.versionName({ label: '', created_at: at }), time)
  assert.equal(c.versionName({ label: 'Draft 1', created_at: at }), 'Draft 1')
  assert.equal(c.versionName({ label: 'Before restore', created_at: at }), 'history.autoRestore')
})

test('the hover card names kind, editors and the commit', () => {
  const c = setup()
  const at = '2026-11-26T13:05:00+00:00'
  const info = s => Object.fromEntries(c.versionInfo(s, { git: true, remote: true }))
  const auto = info({ label: '', kind: 'auto', created_at: at, editors: 'alice,bob', commit_hash: '0123456789abcdef', pushed: false })
  assert.equal(auto['history.infoKind'], 'history.autoName')
  assert.equal(auto['history.infoEditors'], 'alice, bob')
  assert.equal(auto['history.infoCommit'], '0123456 · history.notPushed')
  const manual = info({ label: 'Draft', kind: 'manual', created_at: at, editors: '', commit_hash: '' })
  assert.equal(manual['history.infoKind'], 'history.kindManual')
  assert.equal(manual['history.infoEditors'], 'history.nobody')
  assert.equal(manual['history.infoCommit'], 'history.notCommitted')
  const doc = info({ label: '', created_at: at })  // document versions: no kind, editors or commit
  assert.deepEqual(Object.keys(doc), ['history.infoName', 'history.infoWhen'])
})
