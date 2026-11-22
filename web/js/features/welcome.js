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
function welcomeRow(x) { // one recent document: icon, title, project, time - like a sidebar row
  const row = el('button', 'wRow'); row.type = 'button'
  const ic = el('span', 'wIco'); ic.innerHTML = welcomeIcon(wIco('doc'), 14)
  const p = x.project_id && projects.find(q => q.id === x.project_id)
  const where = x.owner && x.owner !== user ? t('docs.wSharedBy', { owner: x.owner }) : p && p.title !== x.title ? p.title : ''
  const main = el('span', 'wRowMain'); main.append(el('span', 'wRowTitle', x.title))
  if (where) main.append(el('span', 'wRowSub', where))
  row.append(ic, main, el('span', 'wRowWhen', x.updated_at ? ago(x.updated_at) : ''))
  row.onclick = () => openDoc(x.id)
  return row
}
function welcomeRecentCard(n) {
  const card = el('section', 'wCard'); card.setAttribute('aria-labelledby', 'wRecentH')
  const h = el('h2', 'wCardHead', t('docs.wRecent')); h.id = 'wRecentH'
  card.append(h)
  const docs = welcomeRecent(n)
  if (docs.length) docs.forEach(x => card.append(welcomeRow(x)))
  else card.append(el('p', 'wEmpty', t('docs.wEmpty')))
  return card
}
function welcomeInbox() { // unread notifications as one quiet line (opens the inbox)
  let unread = 0
  try { unread = inboxUnread || 0 } catch (e) {} // boot: inbox.js has not run yet
  if (!unread) return null
  const b = welcomeButton(t(unread === 1 ? 'docs.wInboxOne' : 'docs.wInboxMany', { n: unread }), 'bell', () => $('inboxBtn').click(), 'wInbox')
  b.onclick = e => { e.stopPropagation(); $('inboxBtn').click() }
  return b
}
function paintWelcome() {
  const box = $('welcome')
  if (!box || !welcomeOn()) return
  const inner = el('div', 'wInner')
  const head = el('div', 'wHead')
  head.append(el('h1', 'wTitle', t('docs.wTitle')), el('p', 'wSub', t('docs.wLead')))
  const acts = el('div', 'wActs')
  const create = welcomeButton(t('docs.wNew'), 'new', () => newProjectFromMenu(), 'wBtn'); create.classList.add('wPrimary')
  acts.append(create, welcomeButton(t('docs.wImport'), 'upload', () => $('docPick').click(), 'wBtn'))
  const tut = lastOwn.find(x => x.title === 'Tutorial')
  if (tut) acts.append(welcomeButton(t('docs.wTutorial'), 'book', () => openDoc(tut.id), 'wBtn'))
  inner.append(head, acts)
  const inbox = welcomeInbox()
  if (inbox) inner.append(inbox)
  inner.append(welcomeRecentCard(6))
  box.replaceChildren(inner)
}
