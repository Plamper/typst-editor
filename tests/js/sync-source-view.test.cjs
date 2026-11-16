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

test('only lowercase http(s) is a URL; another scheme leaves a real comment', () => {
  assert.equal(ctx.srcViewScan('a://b tail').lines[0].s, 'a:')
  assert.equal(ctx.srcViewScan('foo:// bar tail').lines[0].s, 'foo:')
  assert.equal(ctx.srcViewScan('HTTPS://example.com/x tail').lines[0].s, 'HTTPS:')
  assert.equal(ctx.srcViewScan('mailto:foo@example.com tail').lines[0].s, 'mailto:foo@example.com tail')
})

test('a URL keeps its own slashes, stars and brackets as literal text', () => {
  assert.equal(ctx.srcViewScan('Text https://example.com/x//y tail.').lines[0].s, 'Text https://example.com/x//y tail.')
  assert.equal(ctx.srcViewScan('https://x.example/*y* tail').lines[0].s, 'https://x.example/*y* tail')
  assert.equal(ctx.srcViewScan('https://x.example/a[b] tail').lines[0].s, 'https://x.example/a[b] tail')
  const view = ctx.srcViewScan('https://x.example/*y* tail\nnext line stays')
  assert.equal(view.lines[1].s, 'next line stays') // a /* inside a URL never opens a block comment
  assert.equal(ctx.srcViewScan('#box[https://x.example/*y*] tail').lines[0].s, 'https://x.example/*y* tail')
})

test('a real comment after a URL still ends the line, a URL after a comment stays hidden', () => {
  assert.equal(ctx.srcViewScan('See https://x.example // note').lines[0].s, 'See https://x.example')
  assert.equal(ctx.srcViewScan('// see https://x.example').lines[0].s, '')
})

test('structure scans blank a URL without letting its markup leak into the document', () => {
  assert.doesNotMatch(ctx.srcMask('a://b #image("dot.png")'), /#image/) // the comment hides the call
  assert.match(ctx.srcMask('See https://x.example//p and #image("dot.png")'), /#image\("dot\.png"\)/)
  assert.match(ctx.srcMask('https://x.example/*y*/z #image("dot.png")'), /#image\("dot\.png"\)/) // URL is not a block comment
  assert.doesNotMatch(ctx.srcMask('See https://x.example // #image("dot.png")'), /#image/) // a real comment after the URL
})
