// Start page while no document is open: quick actions and the recently edited documents, in the app's own style.
// Only function declarations: closeView() paints it during boot, before later files' top level has run.
function welcomeOn() { return document.body.classList.contains('welcome') }
function setWelcome(on) {
  if (welcomeOn() === on) { if (on) paintWelcome(); return }
  document.body.classList.toggle('welcome', on)
  $('welcome').hidden = !on
  if (on) paintWelcome()
}
function welcomeIcon(path, size = 15) {
  return `<svg width="${size}" height="${size}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`
}
function wIco(name) { // a function, not a table: closeView() paints during boot, before a const here would exist
  return {
  new: '<path d="M8 3.5v9M3.5 8h9"/>',
  upload: '<path d="M8 10.5V2.5"/><path d="M4.8 5.5L8 2.3l3.2 3.2"/><path d="M2.5 10v2.3c0 .8.7 1.5 1.5 1.5h8c.8 0 1.5-.7 1.5-1.5V10"/>',
  book: '<path d="M2 3.5c2-.8 4-.8 6 .6 2-1.4 4-1.4 6-.6v9c-2-.8-4-.8-6 .6-2-1.4-4-1.4-6-.6z"/><path d="M8 4.1v9"/>',
  doc: '<path d="M4 1.8h5.2L12.5 5v9.2H4z"/><path d="M9 1.8V5h3.5"/>',
  bell: '<path d="M8 2.2a4 4 0 0 0-4 4c0 3-1.5 4.3-1.5 4.3h11S12 9.2 12 6.2a4 4 0 0 0-4-4z"/><path d="M6.5 13a1.5 1.5 0 0 0 3 0"/>',
  }[name]
}
function welcomeButton(label, icon, run, cls) {
  const b = el('button', cls); b.type = 'button'
  const ic = el('span', 'wIco'); ic.innerHTML = welcomeIcon(wIco(icon))
  b.append(ic, el('span', 'wLbl', label))
  b.onclick = () => Promise.resolve().then(run).catch(e => toast(e.message))
  return b
}
function welcomeRecent(n) { // newest first across own and shared documents
  const seen = new Set(), all = []
  for (const x of [...lastOwn, ...lastShared]) if (!seen.has(x.id)) { seen.add(x.id); all.push(x) }
  return all.sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || ''))).slice(0, n)
}
function welcomeRow(icon, title, sub, when, open) { // one line like a sidebar row: icon, title, muted place, time, arrow on hover
  const row = el('button', 'wRow'); row.type = 'button'
  const ic = el('span', 'wIco'); ic.innerHTML = icon
  const main = el('span', 'wRowMain'); main.append(el('span', 'wRowTitle', title))
  if (sub) main.append(el('span', 'wRowSub', sub))
  const go = el('span', 'wGo'); go.innerHTML = welcomeIcon('<path d="M6 3.5L10.5 8 6 12.5"/>', 12)
  row.append(ic, main, el('span', 'wRowWhen', when), go)
  row.onclick = () => Promise.resolve().then(open).catch(e => toast(e.message))
  return row
}
function welcomeCard(id, title, rows, more) {
  const card = el('section', 'wCard'); card.setAttribute('aria-labelledby', id)
  const head = el('div', 'wCardHeadRow')
  const h = el('h2', 'wCardHead', title); h.id = id
  head.append(h)
  if (more) head.append(more)
  card.append(head, ...rows)
  return card
}
function welcomeInboxCard() { // up to three unread notifications, each opens its place; null when nothing is unread
  let items = []
  try { items = inboxItems.filter(it => !it.is_read).slice(0, 3) } catch (e) {} // boot: inbox.js has not run yet
  if (!items.length) return null
  const all = el('button', 'wMore', t('docs.wInboxAll')); all.type = 'button'
  all.onclick = e => { e.stopPropagation(); $('inboxBtn').click() }
  const rows = items.map(it => welcomeRow(it.type === 'mention' ? '<b class="wAt">@</b>' : welcomeIcon(wIco('bell'), 14),
    inboxText(it), it.type === 'mention' ? it.text : '', ago(it.created_at), () => goInbox(it)))
  return welcomeCard('wInboxH', t('docs.wForYou'), rows, all)
}
function welcomeKeys(e) { // arrows walk the rows like a list
  if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
  const rows = [...$('welcome').querySelectorAll('.wRow, .wDocTile')], i = rows.indexOf(document.activeElement)
  if (i < 0) return
  e.preventDefault()
  rows[Math.max(0, Math.min(rows.length - 1, i + (e.key === 'ArrowDown' ? 1 : -1)))].focus()
}
function welcomeDocTile(x) { // a recent document as a small card
  const p = x.project_id && projects.find(q => q.id === x.project_id)
  const b = el('button', 'wDocTile'); b.type = 'button'
  const ic = el('span', 'wIco'); ic.innerHTML = welcomeIcon(wIco('doc'), 14)
  const place = x.owner && x.owner !== user ? t('docs.wSharedBy', { owner: x.owner }) : [p && p.title !== x.title ? p.title : '', x.folder || ''].filter(Boolean).join(' / ')
  b.append(ic, el('span', 'wRowTitle', x.title), el('span', 'wRowSub', place || '\u00a0'),
    el('span', 'wRowWhen', x.updated_at ? ago(x.updated_at) : ''))
  b.onclick = () => openDoc(x.id)
  return b
}
function paintWelcome() {
  const box = $('welcome')
  if (!box || !welcomeOn()) return
  const inner = el('div', 'wInner')
  const head = el('div', 'wHead')
  const titleRow = el('div', 'wTitleRow'); titleRow.append(el('h1', 'wTitle', t('docs.wTitle')))
  head.append(titleRow, el('p', 'wSub', t('docs.wLead')))
  const acts = el('div', 'wActs')
  const create = welcomeButton(t('docs.wNew'), 'new', () => newProjectFromMenu(), 'wBtn'); create.classList.add('wPrimary')
  acts.append(create, welcomeButton(t('docs.wImport'), 'upload', () => $('docPick').click(), 'wBtn'))
  const tut = lastOwn.find(x => x.title === 'Tutorial')
  if (tut) titleRow.append(welcomeButton(t('docs.wTutorial'), 'book', () => openDoc(tut.id), 'wLink'))
  inner.append(head, acts)
  const inbox = welcomeInboxCard()
  if (inbox) inner.append(inbox)
  const docs = welcomeRecent(6)
  const sec = el('section', 'wSection'); sec.setAttribute('aria-labelledby', 'wRecentH')
  const h = el('h2', 'wCardHead wSecHead', t('docs.wRecent')); h.id = 'wRecentH'
  const grid = el('div', 'wDocGrid'); docs.forEach(x => grid.append(welcomeDocTile(x)))
  sec.append(h, docs.length ? grid : el('p', 'wEmpty', t('docs.wEmpty')))
  inner.append(sec)
  box.onkeydown = welcomeKeys
  box.replaceChildren(inner)
}
