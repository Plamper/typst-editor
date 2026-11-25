// Editor drops: one fitting project file, private module or upload lands where it is dropped, with its Typst line.
const IMAGE_EXT = /\.(png|jpe?g|gif|svg|webp|pdf)$/i // what #image() reads
let editorDropMark = null
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
function editorDropLine(data) { // what the drop writes, shown next to the drop mark
  return data.personal ? resLine({ name: data.entry.name }) : mediaInsert(data.entry.name)
}
function editorDropAt(e, inline) { // images go where the pointer is, line statements to the nearest line boundary
  if (inline) return { at: cm.posAtCoords({ x: e.clientX, y: e.clientY }, false), inline }
  const block = cm.lineBlockAtHeight(e.clientY - cm.documentTop), doc = cm.state.doc
  const after = e.clientY > cm.documentTop + block.top + block.height / 2
  return { at: after ? Math.min(block.to + 1, doc.length) : block.from, inline }
}
function editorDropShow(e, inline, text) {
  const { at } = editorDropAt(e, inline), c = cm.coordsAtPos(at, 1)
  if (!c) return editorDropHide()
  if (!editorDropMark) { editorDropMark = el('div', 'editorDrop'); document.body.appendChild(editorDropMark) }
  const doc = cm.state.doc, below = !inline && at === doc.length && doc.length && doc.sliceString(at - 1, at) !== '\n' // the line goes after the last one
  const box = cm.contentDOM.getBoundingClientRect()
  editorDropMark.classList.toggle('inline', inline)
  editorDropMark.style.left = (inline ? c.left : box.left) + 'px'
  editorDropMark.style.top = (below ? c.bottom : c.top) + 'px'
  editorDropMark.style.width = inline ? '' : box.width + 'px'
  editorDropMark.style.height = inline ? c.bottom - c.top + 'px' : ''
  editorDropMark.replaceChildren(...text ? [el('span', null, text)] : [])
  $('editWrap').classList.add('editor-drop')
}
function editorDropHide() {
  editorDropMark?.remove(); editorDropMark = null
  $('editWrap').classList.remove('editor-drop')
}
function editorDropInsert(at, text, inline) {
  const doc = cm.state.doc
  const ins = inline ? text : at === doc.length && doc.length && doc.sliceString(at - 1, at) !== '\n' ? '\n' + text : text + '\n'
  cm.dispatch({ changes: { from: at, insert: ins }, selection: { anchor: at + ins.length } })
  cm.focus()
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
  if (files) editorDropShow(e, true, '')
  else editorDropShow(e, sidebarDragData.personal ? false : IMAGE_EXT.test(sidebarDragData.entry.name), editorDropLine(sidebarDragData))
}
$('editWrap').addEventListener('dragover', editorDropOver, true)
$('editWrap').addEventListener('dragenter', editorDropOver, true)
$('editWrap').addEventListener('dragleave', e => { if (!$('editWrap').contains(e.relatedTarget)) editorDropHide() })
document.addEventListener('dragend', editorDropHide)
$('editWrap').addEventListener('drop', async e => {
  if (!editorDropTypes(e)) return
  e.preventDefault(); e.stopPropagation(); editorDropHide()
  if (!docId || docRole === 'reviewer') return
  if (!e.dataTransfer.files.length) {
    const data = JSON.parse(e.dataTransfer.getData(SIDEBAR_DRAG) || 'null')
    if (!editorDropOpen() || !editorDropFits(data)) return
    const { at, inline } = editorDropAt(e, !data.personal && IMAGE_EXT.test(data.entry.name))
    if (!data.personal) { editorDropInsert(at, mediaInsert(data.entry.name), inline); return }
    const tpl = myTpl.find(r => r.name === data.entry.name)
    if (!tpl) { toast(t('files.tplGone')); return }
    cm.dispatch({ selection: { anchor: at } }) // the module line goes in at the cursor once the copy is in the project
    try { await insertPersonalResource(tpl) } catch (error) { toast(error.message) }
    return
  }
  const single = e.dataTransfer.files.length === 1 && editorDropOpen()
  if (single) cm.dispatch({ selection: { anchor: editorDropAt(e, true).at } })
  let last = ''
  for (const f of e.dataTransfer.files) {
    try {
      if (TEXT_UP.test(f.name)) { last = await upText(f) || last; continue }
      const name = await upFile(f)
      if (name && single && IMAGE_EXT.test(name)) insImage(name)
    } catch (err) { toast(err.message) }
  }
  if (last) switchTab(last) // text drop: show file at once
  queueRender()
}, true)
