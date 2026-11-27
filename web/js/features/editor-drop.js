// Editor drops: one fitting project file, private module or upload lands where it is dropped, with its Typst line.
const IMAGE_EXT = /\.(png|jpe?g|gif|svg|webp|pdf)$/i // what #image() reads
function editorDropOpen() { // a .typ text in the editor that this user may change
  return !!docId && !tplName && !openedTrashed && docRole !== 'reviewer' && tabKind(activeFile) === 'typ' &&
    !$('cmWrap').hidden && cm.state.facet(EditorView.editable)
}
function editorDropFits(data) { // one entry only; project paths resolve from the open document's project alone
  if (!data?.entry || data.items || data.trashed) return false
  if (data.personal) return data.entry.kind === 'resource' && !!docProject && !docResReadonly
  const name = data.entry.name
  return data.project === docProject && data.entry.kind === 'file' && name !== activeFile && (IMAGE_EXT.test(name) || FONT_EXT.test(name) || TEXT_EXT.test(name))
}
function editorDropLine(data) { // what the drop writes, shown in the chip at the drop spot
  return data.personal ? resLine({ name: data.entry.name }) : mediaInsert(data.entry.name)
}
function editorDropAt(e, inline) { // images go where the pointer is, line statements to the nearest line boundary
  if (inline) return { at: cm.posAtCoords({ x: e.clientX, y: e.clientY }, false), inline }
  const block = cm.lineBlockAtHeight(e.clientY - cm.documentTop), doc = cm.state.doc
  const after = e.clientY > cm.documentTop + block.top + block.height / 2
  return { at: after ? Math.min(block.to + 1, doc.length) : block.from, inline }
}
let editorDropMark = null, editorDropSpot = null, editorGlowT = 0
function editorDropShow(e, inline, text, kind) { // a caret (inline) or insertion line, with a chip of the text it writes; text never shifts while dragging
  const { at } = editorDropAt(e, inline), c = cm.coordsAtPos(at, 1)
  if (!c) return editorDropHide()
  editorDropSpot = { at, inline }
  const wrap = $('editWrap').getBoundingClientRect(), box = cm.contentDOM.getBoundingClientRect(), doc = cm.state.doc
  const fresh = !editorDropMark
  if (fresh) {
    editorDropMark = el('div', 'editorDrop')
    editorDropMark.append(el('span', 'edCaret'), el('span', 'edChip'))
    $('editWrap').appendChild(editorDropMark)
  }
  const [caret, chip] = editorDropMark.children
  if (chip.dataset.text !== text) {
    const icon = el('span', 'ti'); icon.dataset.kind = kind
    chip.replaceChildren(icon, el('code', null, text)); chip.dataset.text = text
  }
  editorDropMark.classList.toggle('inline', inline)
  const below = !inline && at === doc.length && doc.length && doc.sliceString(at - 1, at) !== '\n' // a line after the last one goes under it
  const x = (inline ? c.left : box.left + 1) - wrap.left, y = (inline ? c.top : below ? c.bottom : c.top) - wrap.top
  caret.style.transform = `translate(${x}px, ${y}px)`
  caret.style.width = inline ? '' : Math.max(0, box.width - 20) + 'px'
  caret.style.height = inline ? c.bottom - c.top + 'px' : ''
  const cw = chip.offsetWidth, cx = Math.max(8, Math.min(x - 6, wrap.width - cw - 8)) // just under the spot, inside the editor
  const cy = y + (inline ? c.bottom - c.top : 0) + 6
  chip.style.transform = `translate(${cx}px, ${cy}px)`
  if (fresh) requestAnimationFrame(() => editorDropMark?.classList.add('on')) // glide from here on, the first spot appears in place
}
function editorDropHide() {
  editorDropMark?.remove(); editorDropMark = null; editorDropSpot = null
}
function editorDropGlow(from, to) { // the inserted text lights up once and fades, so the eye finds it
  clearTimeout(editorGlowT)
  if (to <= from) return
  cm.dispatch({ effects: setDropPreview.of(Decoration.set(Decoration.mark({ class: 'cm-dropGlow' }).range(from, to))) })
  editorGlowT = setTimeout(() => cm.dispatch({ effects: setDropPreview.of(Decoration.none) }), 1200)
}
function editorDropInsert(at, text, inline) {
  const doc = cm.state.doc, lead = !inline && at === doc.length && doc.length && doc.sliceString(at - 1, at) !== '\n'
  const ins = inline ? text : lead ? '\n' + text : text + '\n'
  cm.dispatch({ changes: { from: at, insert: ins }, selection: { anchor: at + ins.length } })
  cm.focus()
  editorDropGlow(at + (lead ? 1 : 0), at + (lead ? 1 : 0) + text.length)
}
const editorDropTypes = e => e.dataTransfer.types.includes('Files') || e.dataTransfer.types.includes(SIDEBAR_DRAG)
function editorDropOver(e) { // capture phase: CodeMirror would paste a dropped file's bytes as text
  if (!editorDropTypes(e)) return // text drags inside the editor stay with CodeMirror
  e.stopPropagation()
  const files = e.dataTransfer.types.includes('Files')
  if (!files && !(editorDropOpen() && editorDropFits(sidebarDragData))) { editorDropHide(); return } // no preventDefault: the pointer shows the drop is refused
  e.preventDefault(); e.dataTransfer.dropEffect = 'copy'
  const items = e.dataTransfer.items // an upload only shows its spot when it is one image: everything else is just uploaded
  if (!editorDropOpen() || files && (items?.length !== 1 || !/^(image\/|application\/pdf$)/.test(items[0].type))) { editorDropHide(); return }
  if (files) { editorDropShow(e, true, '#image(…)', (items[0].type.split('/')[1] || '').replace('svg+xml', 'svg').replace('jpeg', 'jpg')); return }
  const name = sidebarDragData.entry.name
  editorDropShow(e, !sidebarDragData.personal && IMAGE_EXT.test(name), editorDropLine(sidebarDragData), tabKind(name))
}
$('editWrap').addEventListener('dragover', editorDropOver, true)
$('editWrap').addEventListener('dragenter', editorDropOver, true)
$('editWrap').addEventListener('dragleave', e => { if (!$('editWrap').contains(e.relatedTarget)) editorDropHide() })
document.addEventListener('dragend', editorDropHide)
$('editWrap').addEventListener('drop', async e => {
  if (!editorDropTypes(e)) return
  e.preventDefault(); e.stopPropagation()
  const spot = editorDropSpot // the spot the mark showed, even if the pointer slipped on release
  editorDropHide()
  if (!docId || docRole === 'reviewer') return
  if (!e.dataTransfer.files.length) {
    const data = JSON.parse(e.dataTransfer.getData(SIDEBAR_DRAG) || 'null')
    if (!editorDropOpen() || !editorDropFits(data)) return
    const { at, inline } = spot || editorDropAt(e, !data.personal && IMAGE_EXT.test(data.entry.name))
    if (!data.personal) { editorDropInsert(at, mediaInsert(data.entry.name), inline); return }
    const tpl = myTpl.find(r => r.name === data.entry.name)
    if (!tpl) { toast(t('files.tplGone')); return }
    cm.dispatch({ selection: { anchor: at } }) // the module line goes in at the cursor once the copy is in the project
    try { await insertPersonalResource(tpl) } catch (error) { toast(error.message) }
    editorDropGlow(at, cm.state.selection.main.head - 1)
    return
  }
  const single = e.dataTransfer.files.length === 1 && editorDropOpen()
  if (single) cm.dispatch({ selection: { anchor: (spot || editorDropAt(e, true)).at } })
  let last = ''
  for (const f of e.dataTransfer.files) {
    try {
      if (TEXT_UP.test(f.name)) { last = await upText(f) || last; continue }
      const name = await upFile(f)
      if (name && single && IMAGE_EXT.test(name)) { const at = cm.state.selection.main.from; insImage(name); editorDropGlow(at, cm.state.selection.main.head) }
    } catch (err) { toast(err.message) }
  }
  if (last) switchTab(last) // text drop: show file at once
  queueRender()
}, true)
