const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const settings = fs.readFileSync('web/js/90-settings.js', 'utf8')
const srcMath = settings.slice(settings.indexOf('function pvSrcMath'), settings.indexOf('function pvAlign'))
const mathMatch = settings.slice(settings.indexOf('function pvMathMatch'), settings.indexOf('function pvMathIndex'))

test('a repeated cursor move reuses the source formula list, an edit rebuilds it', () => {
  const state = { srcText: '= T\n$ x + y $\n', scans: 0 }
  const c = vm.createContext({
    pvSrcList: () => [{ key: '', text: state.srcText }],
    pvMainText: () => state.srcText,
    pvFileAnchor: () => 0,
    srcViewOf: text => ({ lines: [{ line: 1, s: '', map: [] }, { line: 2, s: text.includes('z') ? 'x+z' : 'x+y', map: [0, 1, 2] }], math: [] }),
    srcObjs: () => { state.scans++; return { formulas: [{ line: 2, end: 2 }] } },
    pvMathNorm: s => s,
  })
  vm.runInContext(srcMath, c)
  const one = c.pvSrcMath(), again = c.pvSrcMath()
  assert.equal(one, again) // same array: no rescan while the text is unchanged
  assert.equal(state.scans, 1)
  state.srcText = '= T\n$ x + z $\n'
  const fresh = c.pvSrcMath()
  assert.notEqual(one, fresh)
  assert.equal(state.scans, 2)
  assert.equal(fresh[0].s, 'x+z')
})

test('a repeated cursor move reuses the math match, a new render or formula rebuilds it', () => {
  const formula = { key: '', line: 3, end: 3, s: 'x+y', own: [{ line: 3, at: 0 }], order: 3 }
  const state = { src: [formula], cands: 0 }
  const line = { s: 'x+y', own: [{ x0: 0, w: 5 }], x0: 0, x1: 20, y0: 10, y1: 20, vw: 100, vh: 100 }
  const c = vm.createContext({
    cm: { state: { doc: { lines: 20 } } },
    pvTexts: [[{}]],
    pvOutlineGen: 1,
    shownN: 1,
    pvSrcMath: () => state.src,
    pvMathLines: () => [line],
    pvPageHint: () => 2,
    pvSecAtY: () => null,
    pvMathCands: (L, hint, srcs) => { state.cands++; return [{ src: srcs[0], map: [0, 1, 2], ratio: 0, score: 0 }] },
  })
  vm.runInContext(mathMatch, c)
  const one = c.pvMathMatch(), again = c.pvMathMatch()
  assert.equal(one, again) // same array: no alignment while page and formulas are unchanged
  assert.equal(state.cands, 1)
  c.pvTexts = [[{}]]
  c.shownN = 2
  const rendered = c.pvMathMatch()
  assert.notEqual(one, rendered) // a render swaps the page items
  assert.equal(state.cands, 2)
  state.src = [{ ...formula, s: 'x+z' }]
  const edited = c.pvMathMatch()
  assert.notEqual(rendered, edited) // the formula changed
  assert.equal(state.cands, 3)
})
