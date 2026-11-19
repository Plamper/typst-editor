const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const read = name => fs.readFileSync('web/js/' + name, 'utf8')
const settings = read('90-settings.js')
const preview = read('50-preview.js')
const slice = (src, a, b) => {
  const i = src.indexOf(a), j = src.indexOf(b, i)
  assert.ok(i >= 0 && j > i, 'missing slice ' + a)
  return src.slice(i, j)
}
const posOfLine = (t, ln) => { let p = 0; for (let i = 1; i < ln; i++) p = t.indexOf('\n', p) + 1; return p }

function viewCtx(srcs) {
  const c = vm.createContext({ posOfLine, srcList: srcs || [] })
  vm.runInContext(slice(preview, 'function srcMask', 'function srcHeadings'), c)
  vm.runInContext(slice(settings, 'const SRC_MATH', 'function srcObjs'), c)
  vm.runInContext(slice(settings, 'function srcLineWords', 'function syncWords'), c)
  vm.runInContext(slice(settings, 'function pvWordChar', 'function locateNear'), c)
  vm.runInContext(slice(settings, 'function pvClickedWord', 'function pvMainText'), c)
  vm.runInContext('function pvSrcList() { return srcList }', c)
  return c
}

test('a line-break hyphen the PDF text layer drops still finds the source word and letter', () => {
  const src = 'Die Rechtsschutzversicherungsgesellschaften verhandeln Arbeitsunfähigkeitsbescheinigungen täglich.'
  const c = viewCtx()
  const needle = 'Die Rechtsschutzversicherungsgesellschaften verhandeln Arbeitsunfä higkeitsbescheinigungen täglich.' // Typst 0.15: the hyphen glyph is no text
  const off = needle.indexOf('higkeits') + 2 // the "k" of the second half
  const at = c.pvViewPos(src, needle, off, 1)
  assert.equal(at, src.indexOf('higkeits') + 2)
  assert.equal(c.pvViewPos(src, needle, needle.indexOf('Arbeits') + 1, 1), src.indexOf('Arbeits') + 1)
  assert.equal(c.pvViewPos('The therapist lineup is long enough here.', 'herapist lineup is long enoug', 0, 1), -1) // still whole words on both ends, never a word fragment
})

test('text a function or loop builds maps to the string or definition that holds the word', () => {
  const src = '#let greet(name) = [Hello #name friend]\n= Functions\n#note[Custom note body sentence.]\n#greet("Wanda")\n#for p in ("Ada", "Grace") [Person #p listed.]\n// Wanda in a comment'
  const c = viewCtx()
  const at = c.pvCodeWord(src, { s: 'Hello Wanda friend', off: 7 }, 3)
  assert.equal(at, src.indexOf('Wanda') + 1) // the call site, letter kept; the comment never counts
  assert.equal(c.pvCodeWord(src, { s: 'Hello Wanda friend', off: 1 }, 3), src.indexOf('Hello') + 1) // the definition owns its own words
  assert.equal(c.pvCodeWord(src, { s: 'Person Grace listed.', off: 8 }, 5), src.indexOf('Grace') + 1)
  assert.equal(c.pvCodeWord(src, { s: 'Nobody here', off: 1 }, 1), -1)
})

test('a word that exists once across all sources owns the click (bibliography fields in another order)', () => {
  const bib = '@book{knuth,\n  title={The TeXbook},\n  author={Knuth, Donald},\n  year={1984},\n  publisher={Addison-Wesley}\n}\n'
  const main = 'As shown by @knuth, typesetting matters.\n#bibliography("refs.bib")'
  const c = viewCtx([{ key: '', text: main }, { key: 'refs.bib', text: bib }])
  const hit = c.pvUniqueWord({ s: '. Addison-Wesley, 1984.', off: 3 })
  assert.equal(hit.key, 'refs.bib')
  assert.equal(hit.at, bib.indexOf('Addison') + 1)
  assert.equal(c.pvUniqueWord({ s: 'D. Knuth, The TeXbook', off: 4 }), null) // "knuth" is the key and the author: not unique
  const any = c.pvUniqueAny({ s: 'D. Knuth, The TeXbook. Addison-Wesley, 1984.', off: 0 })
  assert.equal(any.at, bib.indexOf('TeXbook'))
})

test('a short formula that shares three glyphs with a longer display line is no candidate', () => {
  const c = vm.createContext({})
  vm.runInContext(slice(settings, 'function pvAlign', 'function pvMathMatch'), c)
  const srcs = [
    { key: '', line: 2, end: 2, s: 'a2+b2=c2', own: [], order: 2 }, // inline math in a paragraph above
    { key: '', line: 10, end: 11, s: 'f(x)=x2+2x+1=(x+1)2', own: [], order: 10 },
  ]
  const L = { s: 'f(x)=x2+2x+1' }
  const cands = c.pvMathCands(L, 3, srcs) // the page hint sits right next to the inline formula
  assert.equal(cands[0].src.line, 10)
  assert.ok(!cands.some(k => k.src.line === 2), 'a²+b²=c² must not explain x²+2x+1')
  const wide = c.pvMathCands({ s: 'a2+b2=c2x+y' }, 2, srcs) // a real grid row: the whole cell formula is there
  assert.equal(wide[0].src.line, 2)
})

test('an undrawn page loads its fonts before math detection reads their names', () => {
  const c = vm.createContext({})
  vm.runInContext(slice(preview, 'function pvFontsMissing', 'function pvMat'), c)
  const resolved = new Set(['f1'])
  const page = { commonObjs: { get(n) { if (!resolved.has(n)) throw new Error('not resolved'); return { name: n === 'f2' ? 'NewCMMath-Book' : 'Libertinus' } } } }
  const tc = { items: [{ str: 'Text', fontName: 'f1' }, { str: '𝑥', fontName: 'f2' }, { str: ' ', fontName: 'f3' }] }
  assert.equal(c.pvFontsMissing(tc, page), true)
  assert.equal(c.pvFontName(page, 'f2'), '') // what pvBoxes would store without the operator list
  resolved.add('f2')
  assert.equal(c.pvFontsMissing(tc, page), false) // a blank run never forces an operator list
  assert.equal(c.pvFontName(page, 'f2'), 'NewCMMath-Book')
  const meta = slice(preview, 'function pvPageMeta', 'if (pvBox0')
  assert.match(meta, /pvFontsMissing\(tc, page\)\) \{ try \{ await page\.getOperatorList\(\)/)
  assert.ok(meta.indexOf('pvPageImgs(page, v)') < meta.indexOf('pvBoxes(tc, v, page)'), 'the operator list must run before the boxes read the font names')
})
