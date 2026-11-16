const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const read = name => fs.readFileSync('web/js/' + name, 'utf8')
const preview = read('50-preview.js')
const repeatSlice = preview.slice(preview.indexOf('function pvImgRepeat'), preview.indexOf('const PV_LINK_SCHEME'))
const sigSlice = preview.slice(preview.indexOf('function pvImgSig'), preview.indexOf('function pvHasImages'))
const ctx = vm.createContext({})
vm.runInContext(repeatSlice, ctx)

const box = (sig, id) => ({ x0: 20, y0: 40, x1: 120, y1: 90, w: 100, h: 50, id, sig })

test('an image only counts as repeated when its content matches', () => {
  ctx.pvImgs = [[box('same', 'a')], [box('other', 'b')]]
  assert.equal(ctx.pvImgRepeat(0, ctx.pvImgs[0][0]), false) // two different images at the same spot
  ctx.pvImgs[1][0].sig = 'same'
  assert.equal(ctx.pvImgRepeat(0, ctx.pvImgs[0][0]), true) // the logo itself repeats
  ctx.pvImgs = [[{ ...box('', 'a'), sig: undefined }], [{ ...box('', 'b'), sig: undefined }]]
  assert.equal(ctx.pvImgRepeat(0, ctx.pvImgs[0][0]), true) // no fingerprint: keep the old geometry rule
})

test('rendered image boxes carry a content fingerprint', async () => {
  const OPS = { save: 1, restore: 2, transform: 3, paintFormXObjectBegin: 4, paintFormXObjectEnd: 5, paintImageXObject: 6, paintJpegXObject: 7, paintInlineImageXObject: 8, paintImageMaskXObject: 9 }
  const c = vm.createContext({ window: { pdfjsLib: { OPS } }, pdfjsLib: { OPS } })
  vm.runInContext(preview.slice(preview.indexOf('function pvMat'), preview.indexOf('function pvImgRepeat')), c)
  vm.runInContext(sigSlice, c)
  const page = data => ({
    getOperatorList: async () => ({ fnArray: [OPS.transform, OPS.paintImageXObject], argsArray: [[10, 0, 0, 10, 0, 0], ['img1']] }),
    objs: { get: () => ({ width: 32, height: 12, kind: 2, data }) },
  })
  const view = { transform: [1, 0, 0, 1, 0, 0], width: 100, height: 100 }
  const one = await c.pvPageImgs(page(new Uint8Array([1, 2, 3, 4, 5, 6])), view)
  const again = await c.pvPageImgs(page(new Uint8Array([1, 2, 3, 4, 5, 6])), view)
  const other = await c.pvPageImgs(page(new Uint8Array([1, 2, 3, 9, 5, 6])), view)
  assert.equal(one.length, 1)
  assert.ok(one[0].sig)
  assert.equal(one[0].sig, again[0].sig)
  assert.notEqual(one[0].sig, other[0].sig)
  assert.equal(c.pvImgSig({ objs: { get: () => { throw new Error('gone') } } }, 'img1'), '')
})
