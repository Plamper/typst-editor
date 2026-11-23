async function paintImageFile(name) {
  let imageFile = $('imageFile')
  if (!imageFile) { imageFile = el('div', 'imageFile'); imageFile.id = 'imageFile'; $('editWrap').appendChild(imageFile) }
  const pdf = /\.pdf$/i.test(name), font = FONT_EXT.test(name)
  const on = !!docId && !!activeFile && !tplName && (pdf || font || /\.(png|jpe?g|gif|webp|svg|avif|bmp|ico)$/i.test(name))
  imageFile.hidden = !on
  const clear = () => {
    if (imageFile.dataset.url) URL.revokeObjectURL(imageFile.dataset.url)
    if (imageFile.fontFace) document.fonts.delete(imageFile.fontFace)
    delete imageFile.dataset.url; imageFile.fontFace = null; imageFile.replaceChildren()
  }
  if (!on) {
    if (imageFile.dataset.key && !/\.(bib|csv|tsv)$/i.test(name)) $('cmWrap').hidden = false
    imageFile.dataset.key = ''; clear(); return
  }
  const version = lastFiles.find(f => f.path === name)?.version
  const session = window.changeSession
  const proposed = session?.files.get(sessionKey({ kind: 'file', path: name }))
  const key = docProject + '/' + name + '@' + (version || '') + (session ? ':' + session.previewRevision + ':' + session.original : '')
  if (key === imageFile.dataset.key) {
    if (imageFile.dataset.failed) { imageFile.dataset.failed = '1'; imageFile.hidden = true; $('cmWrap').hidden = false }
    else $('cmWrap').hidden = true
    return
  }
  $('cmWrap').hidden = true
  delete imageFile.dataset.failed
  clear()
  imageFile.dataset.key = key
  const view = el(pdf ? 'embed' : 'img')
  if (pdf) view.type = 'application/pdf'
  else view.alt = name
  view.onerror = () => { if (imageFile.dataset.key === key) { imageFile.dataset.failed = '1'; imageFile.hidden = true; $('cmWrap').hidden = false } }
  try {
    const staged = proposed ? sessionBytes(proposed, session.original || proposed.action === 'delete') : null
    if (proposed && staged == null) { toast(t('packages.fileAbsent')); view.onerror(); return }
    const response = staged ? new Response(staged) : await fetch(fileUrl(name))
    if (!response.ok) throw Error(response.status)
    const bytes = await response.arrayBuffer()
    if (imageFile.dataset.key !== key) return
    if (font) { await paintFontFile(imageFile, name, bytes, key); return }
    const ext = name.split('.').pop().toLowerCase()
    const type = pdf ? 'application/pdf' : ({ jpg: 'image/jpeg', jpeg: 'image/jpeg', svg: 'image/svg+xml', ico: 'image/x-icon' }[ext] || 'image/' + ext)
    imageFile.dataset.url = URL.createObjectURL(new Blob([bytes], { type }))
    view.src = imageFile.dataset.url
    imageFile.replaceChildren(view)
  } catch (e) { view.onerror() }
}
let fontFaceN = 0
const fontRange = (a, z) => Array.from({ length: z.charCodeAt(0) - a.charCodeAt(0) + 1 }, (_, i) => String.fromCharCode(a.charCodeAt(0) + i))
const FONT_SETS = [['files.fontLetters', [...fontRange('A', 'Z'), ...'ÄÖÜ'].map(c => c + c.toLowerCase()).concat('ß')],
  ['files.fontSymbols', [...fontRange('0', '9'), ...'.,:;!?&@#%*+-=/()[]{}<>„“”‚‘’«»–—…€$§']]]
async function paintFontFile(box, name, bytes, key) { // font tab: quiet card like the preview empty state, "Aa" and glyph lists in the font itself
  const family = parseFontFamiliesJS(new Uint8Array(bytes))[0] || name.split('/').pop().replace(/\.[^.]+$/, '')
  const face = new FontFace('typst-editor-font-' + (++fontFaceN), bytes)
  await face.load() // unsupported for the browser (e.g. some collections): throws into the text fallback
  if (box.dataset.key !== key) return
  document.fonts.add(face); box.fontFace = face
  const ff = '"' + face.family + '"'
  const sample = el('div', 'fontSample', 'Aa')
  sample.style.fontFamily = ff
  const wrap = el('div', 'fontFile')
  wrap.append(el('div', 'fontKind', t('editor.fontProject') + ' · ' + name.split('.').pop().toUpperCase()), sample, el('div', 'fontName', family),
    el('div', null, `${name} · ${t('files.sizeKb', { kb: Math.round(bytes.byteLength / 1024) })}`))
  const usage = `#text(font: "${family}")[]`, use = el('div', 'fontUse'), copy = icoBtn('ibtn', ICO_DUP)
  copy.title = t('common.copy'); copy.setAttribute('aria-label', copy.title)
  copy.onclick = () => copyTxt(usage)
  use.append(el('code', null, usage), copy)
  wrap.append(use)
  for (const [label, chars] of FONT_SETS) {
    const set = el('details', 'fontSet'), grid = el('div', 'fontGlyphs')
    grid.style.fontFamily = ff
    grid.append(...chars.map(ch => el('span', null, ch)))
    set.append(el('summary', null, t(label)), grid)
    wrap.append(set)
  }
  box.replaceChildren(wrap)
}
