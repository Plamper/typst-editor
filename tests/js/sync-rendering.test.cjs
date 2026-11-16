const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const read = name => fs.readFileSync('web/js/' + name, 'utf8')
const preview = read('50-preview.js')
const sync = read('40-editor-sync.js')
const settings = read('90-settings.js')

function workerContext() {
  const src = preview.slice(preview.indexOf('const PV_WORKER_SRC = `') + 'const PV_WORKER_SRC = `'.length,
    preview.indexOf('`\nlet pvWorker'))
  const posted = [], state = { resets: 0, mapped: 0, failCompile: false, failMap: false, seen: '' }
  const comp = {
    reset: async () => { state.resets++ },
    compile: async () => {
      if (state.failCompile) return { result: null, diagnostics: [{ severity: 'error', message: 'bad' }] }
      return { result: new Uint8Array([1]), diagnostics: [] }
    }
  }
  const T = {
    resetShadow: async () => { state.mapped = 0 },
    mapShadow: async () => { if (state.failMap) throw new Error('map'); state.mapped++ },
    addSource: async (path, text) => { state.seen = String(text) },
    getCompiler: async () => comp
  }
  const c = vm.createContext({ self: {}, Uint8Array, TextEncoder, TextDecoder, setTimeout,
    postMessage: m => posted.push(m) })
  vm.runInContext(src + '\n;globalThis.__setT = v => { T = v }; globalThis.__run = onmessage;', c)
  c.__setT(T)
  return { c, posted, state }
}

async function work(c, data) {
  c.__run({ data })
  for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve))
}

const job = (id, full) => ({ cmd: 'work', id, full, main: '= Hi', files: [{ name: 'a.png', bytes: [1] }], tpl: [] })

test('a content error after the full remap still reports the shadow as mapped', async () => {
  const { c, posted, state } = workerContext()
  state.failCompile = true
  await work(c, job(1, true))
  assert.equal(posted[0].fail, 'bad')
  assert.equal(posted[0].mapped, true)
  state.failCompile = false
  await work(c, job(2, false))
  assert.equal(posted[1].mapped, true)
  assert.ok(posted[1].pdf)
})

test('a mapping failure leaves the shadow unmapped for the next render', async () => {
  const { c, posted, state } = workerContext()
  state.failMap = true
  await work(c, job(1, true))
  assert.equal(posted[0].fail, 'map')
  assert.equal(posted[0].mapped, false)
})

test('incremental compiles keep the compiler cache, a full remap resets it once', async () => {
  const { c, state } = workerContext()
  await work(c, job(1, true))
  await work(c, job(2, false))
  await work(c, job(3, false))
  assert.equal(state.resets, 1)
  assert.equal(state.mapped, 1) // one remap, not one per keystroke
  assert.equal(state.seen, '= Hi')
})

test('an older finished compile is shown while a newer render is still queued', () => {
  const line = preview.slice(preview.indexOf('const pvStale'), preview.indexOf('\n', preview.indexOf('const pvStale')))
  const c = vm.createContext({ shownN: 2, docId: 'doc', tplName: '' })
  vm.runInContext(line + '\n;globalThis.__stale = pvStale', c)
  assert.equal(c.__stale(3, 'doc', ''), false) // finished after the screen, still useful
  assert.equal(c.__stale(2, 'doc', ''), false) // exactly what is on screen
  assert.equal(c.__stale(1, 'doc', ''), true) // older than the screen
  assert.equal(c.__stale(3, 'other', ''), true) // other document
})

test('block formulas read only their own source lines, never the whole file', () => {
  const src = settings.slice(settings.indexOf('function pvSrcMath'), settings.indexOf('function pvAlign'))
  const lines = [
    { line: 1, s: 'intro', map: [0, 1, 2, 3, 4] },
    { line: 2, s: 'x+1', map: [10, 11, 12] },
    { line: 3, s: '=y', map: [20, 21] },
    { line: 4, s: 'tail', map: [0, 1, 2, 3] },
  ]
  const guarded = new Proxy(lines, { get (target, prop) { // outside the formula: the old scan touched these
    if (prop === '0' || prop === '3') throw new Error('scanned a line outside the formula')
    return target[prop]
  } })
  const c = vm.createContext({
    pvMainText: () => 'main\n', pvSrcList: () => [{ key: '', text: 'src' }], pvFileAnchor: () => 0,
    srcViewOf: () => ({ lines: guarded }), srcObjs: () => ({ formulas: [{ line: 2, end: 3 }] }),
    pvMathNorm: s => s,
  })
  vm.runInContext(src, c)
  const out = c.pvSrcMath()
  assert.equal(out.length, 1)
  assert.equal(out[0].s, 'x+1=y')
  assert.equal(JSON.stringify(out[0].own.map(o => [o.line, o.at])), '[[2,10],[2,11],[2,12],[3,20],[3,21]]')
})


test('equal length drift never applies editor changes to the wrong Yjs positions', () => {
  const slice = sync.slice(sync.indexOf('function pushLocal'), sync.indexOf('function pullRemote'))
  const state = { inserted: false, wrote: '' }
  const c = vm.createContext({
    window: {},
    ydoc: { transact: fn => fn() },
    ytext: { length: 3, toString: () => 'abc', insert: () => { state.inserted = true }, delete: () => {} },
    shadow: 'xyz',
    getT: () => 'xQyz',
    setYText: v => { state.wrote = v }
  })
  vm.runInContext(slice + ';globalThis.__push = pushLocal', c)
  const changes = { iterChanges: cb => cb(1, 1, 1, 1, { toString: () => 'Q' }) }
  c.__push(changes, 3)
  assert.equal(state.inserted, false)
  assert.equal(state.wrote, 'xQyz')
  assert.equal(c.shadow, 'xQyz')
})
