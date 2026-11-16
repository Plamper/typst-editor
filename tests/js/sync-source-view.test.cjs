const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const read = name => fs.readFileSync('web/js/' + name, 'utf8')
const settings = read('90-settings.js')
const preview = read('50-preview.js')
const srcMask = preview.slice(preview.indexOf('function srcMask'), preview.indexOf('function srcHeadings'))
const srcView = settings.slice(settings.indexOf('const SRC_MATH'), settings.indexOf('function srcObjs'))
const ctx = vm.createContext({})
vm.runInContext(srcMask + srcView, ctx)

test('a bare URL stays visible text, a real comment still ends the line', () => {
  const view = ctx.srcViewScan('See https://example.com/path for details.\nA real // comment here')
  assert.equal(view.lines[0].s, 'See https://example.com/path for details.')
  assert.equal(view.lines[1].s, 'A real')
})

test('structure regexes see a call after a bare URL, never inside a real comment', () => {
  assert.match(ctx.srcMask('See https://example.com/path #image("dot.png")'), /#image\("dot\.png"\)/)
  assert.doesNotMatch(ctx.srcMask('See // #image("dot.png")'), /#image/)
})

test('emphasis markers inside a word stay literal text, real delimiters do not', () => {
  const view = ctx.srcViewScan('Snake_case star*mid*word and *emph* text.')
  assert.equal(view.lines[0].s, 'Snake_case star*mid*word and emph text.')
})
