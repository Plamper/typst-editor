const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')

const source = fs.readFileSync('web/js/20-completions.js', 'utf8')
const complete = source.slice(source.indexOf('function typstComplete'), source.indexOf('function typstHover'))

function completionContext(bibKeys, parser, fonts = []) {
  const c = vm.createContext({
    window: {}, tplName: '', activeFile: '', tabName: () => '', bibKeys, mediaNames: [], getT: () => '',
    fileRefs: () => [], availableFonts: () => fonts, t: key => key, T_KW: [], T_SNIP: [], T_BLOCKED: new Set(['LineComment', 'BlockComment', 'Raw', 'Str', 'Label', 'Ref']),
    typstParser: parser, typstBuiltinSignatures: undefined, pcDoc: null, pcTree: null,
  })
  vm.runInContext(complete, c)
  return c
}

function editor(text, pos = text.length) {
  return {
    pos,
    explicit: false,
    state: { doc: { length: text.length, sliceString: (from, to) => text.slice(from, to), toString: () => text } },
    matchBefore: () => null,
  }
}

test('@ offers bibliography keys alongside the vendor document labels', () => {
  const c = completionContext(['smith2020', 'doe2021'])
  const result = c.typstComplete(editor('@'))
  assert.equal(result.from, 1)
  assert.deepEqual(result.options.map(o => o.label), ['smith2020', 'doe2021'])
  assert.equal(c.typstComplete(editor('@doe')).options[0].label, 'doe2021')
})

test('@ stays quiet without bibliography keys or inside comments', () => {
  assert.equal(completionContext([]).typstComplete(editor('@')), null)
  const tree = text => ({ resolveInner: () => ({ name: text, parent: null }) })
  assert.equal(completionContext(['smith2020'], { parse: () => tree('LineComment') }).typstComplete(editor('// @')), null)
})

test('#cite(< completes a label and consumes an existing closing angle bracket', () => {
  const c = completionContext(['smith2020'])
  const result = c.typstComplete(editor('#cite(<smi'))
  assert.equal(result.from, 7)
  assert.equal(result.options[0].label, 'smith2020')

  const changes = []
  const view = { state: { sliceDoc: (from, to) => '#cite(<smi>'.slice(from, to) }, dispatch: tr => changes.push(tr) }
  result.options[0].apply(view, result.options[0], result.from, 10)
  assert.equal(changes.length, 1)
  assert.deepEqual(JSON.parse(JSON.stringify(changes[0])), { changes: { from: 7, to: 11, insert: 'smith2020>' }, selection: { anchor: 17 }, userEvent: 'input.complete' })
})

test('#cite(" does not offer bibliography keys for the invalid string form', () => {
  assert.equal(completionContext(['smith2020']).typstComplete(editor('#cite("smi')), null)
})

const SAMPLE_FONTS = [
  { family: 'Acme Sans', source: 'project' },
  { family: 'Inter', source: 'server' },
  { family: 'Libertinus Serif', source: 'builtin' },
  { family: 'DejaVu Sans Mono', source: 'builtin' },
]

test('font completion covers #set text, #text, bare set text, and auto-quotes', () => {
  const c = completionContext([], undefined, SAMPLE_FONTS)
  // Quoted inside #set text(font: "...")
  const r1 = c.typstComplete(editor('#set text(font: "'))
  assert.equal(r1.from, 16)
  assert.deepEqual([...r1.options.map(o => o.label)], ['Acme Sans', 'Inter', 'DejaVu Sans Mono', 'Libertinus Serif'])
  assert.equal(r1.options[0].detail, 'editor.fontProject')
  assert.equal(r1.options[1].detail, 'editor.fontServer')

  // Apply consumes existing closing quote from closeBrackets
  const changes = []
  const view = { state: { sliceDoc: (f, t) => '#set text(font: "Int")'.slice(f, t) }, dispatch: tr => changes.push(tr) }
  const r2 = c.typstComplete(editor('#set text(font: "Int'))
  assert.equal(r2.from, 16)
  assert.equal(r2.options[0].label, 'Inter')
  r2.options[0].apply(view, r2.options[0], r2.from, 20)
  assert.deepEqual(JSON.parse(JSON.stringify(changes[0])), { changes: { from: 16, to: 21, insert: '"Inter"' }, selection: { anchor: 23 }, userEvent: 'input.complete' })

  // Unquoted after font: auto-quotes
  const r3 = c.typstComplete(editor('#text(12pt, font: Acm'))
  assert.equal(r3.from, 18)
  assert.equal(r3.options[0].label, 'Acme Sans')

  // Bare set text inside #show rule
  const r4 = c.typstComplete(editor('#show raw: set text(font: "Deja'))
  assert.equal(r4.options[0].label, 'DejaVu Sans Mono')
})

test('font completion supports fallback arrays and dictionary forms, deduplicating used fonts', () => {
  const c = completionContext([], undefined, SAMPLE_FONTS)
  const r1 = c.typstComplete(editor('#set text(font: ("Inter", "'))
  assert.equal(r1.options.some(o => o.label === 'Inter'), false)
  assert.equal(r1.options.some(o => o.label === 'Libertinus Serif'), true)

  const r2 = c.typstComplete(editor('#set text(font: (name: "Acm'))
  assert.equal(r2.options[0].label, 'Acme Sans')

  const r3 = c.typstComplete(editor('#set text(font: ((name: "Inter", covers: "latin-in-cjk"), (name: "'))
  assert.equal(r3.options.some(o => o.label === 'Inter'), false)
  assert.equal(r3.options.some(o => o.label === 'Acme Sans'), true)

  // Stays quiet in covers:, other params, other functions, and comments/raw
  assert.equal(c.typstComplete(editor('#set text(font: (name: "Inter", covers: "')), null)
  assert.equal(c.typstComplete(editor('#set text(font: "Inter", fill: "')), null)
  assert.equal(c.typstComplete(editor('#my_text(font: "')), null)
  const tree = name => ({ resolveInner: () => ({ name, parent: null }) })
  assert.equal(completionContext([], { parse: () => tree('LineComment') }, SAMPLE_FONTS).typstComplete(editor('// #set text(font: "')), null)
  assert.equal(completionContext([], { parse: () => tree('Raw') }, SAMPLE_FONTS).typstComplete(editor('`#set text(font: "`')), null)
})
