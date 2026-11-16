const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const read = name => fs.readFileSync('web/js/' + name, 'utf8')
const preview = read('50-preview.js')
const sync = read('40-editor-sync.js')

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
