// Start page while no document is open: a title page with quick actions and the recently edited documents as contents.
// Only function declarations: closeView() paints it during boot, before later files' top level has run.
function welcomeOn() { return document.body.classList.contains('welcome') }
function setWelcome(on) {
  if (welcomeOn() === on) { if (on) paintWelcome(); return }
  document.body.classList.toggle('welcome', on)
  $('welcome').hidden = !on
  if (on) paintWelcome()
}
function welcomeIcon(path) {
  return `<svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`
}
function welcomeAction(label, icon, run, primary) {
  const b = el('button', 'wAct' + (primary ? ' wPrimary' : '')); b.type = 'button'
  const ic = el('span', 'wIco'); ic.innerHTML = welcomeIcon(icon)
  b.append(ic, el('span', null, label))
  b.onclick = () => Promise.resolve().then(run).catch(e => toast(e.message))
  return b
}
function welcomeRecent() { // newest first across own and shared documents
  const seen = new Set(), all = []
  for (const x of [...lastOwn, ...lastShared]) if (!seen.has(x.id)) { seen.add(x.id); all.push(x) }
  return all.sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || ''))).slice(0, 7) // seven contents lines
}
function welcomeLine(x) {
  const row = el('button', 'wLine'); row.type = 'button'
  const p = x.project_id && projects.find(q => q.id === x.project_id)
  const where = x.owner && x.owner !== user ? t('docs.wSharedBy', { owner: x.owner }) : p && p.title !== x.title ? p.title : ''
  const name = el('span', 'wName'); name.append(el('span', 'wDoc', x.title))
  if (where) name.append(el('span', 'wWhere', where))
  row.append(name, el('span', 'wLeader'), el('span', 'wWhen', x.updated_at ? ago(x.updated_at) : ''))
  row.onclick = () => openDoc(x.id)
  return row
}
function paintWelcome() {
  const box = $('welcome')
  if (!box || !welcomeOn()) return
  const hour = new Date().getHours()
  const greet = t(hour < 5 ? 'docs.wNight' : hour < 11 ? 'docs.wMorning' : hour < 18 ? 'docs.wDay' : 'docs.wEvening')
  const name = user ? user.charAt(0).toUpperCase() + user.slice(1) : ''
  let date = ''
  try { date = new Intl.DateTimeFormat(I18N_LANG, { weekday: 'long', day: 'numeric', month: 'long' }).format(new Date()) } catch (e) {}

  const sheet = el('article', 'wSheet')
  const head = el('div', 'wHead')
  head.append(el('p', 'wKicker', date))
  const h = el('h1', 'wTitle', name ? greet + ', ' + name : greet); h.id = 'wTitle'
  head.append(h, el('p', 'wLead', t('docs.wLead')))
  sheet.append(head)

  const acts = el('div', 'wActs')
  acts.append(welcomeAction(t('docs.wNew'), '<path d="M8 3.5v9M3.5 8h9"/>', () => newProjectFromMenu(), true))
  acts.append(welcomeAction(t('docs.wImport'), '<path d="M8 10.5V2.5"/><path d="M4.8 5.5L8 2.3l3.2 3.2"/><path d="M2.5 10v2.3c0 .8.7 1.5 1.5 1.5h8c.8 0 1.5-.7 1.5-1.5V10"/>', () => $('docPick').click()))
  const tut = lastOwn.find(x => x.title === 'Tutorial')
  if (tut) acts.append(welcomeAction(t('docs.wTutorial'), '<path d="M2 3.5c2-.8 4-.8 6 .6 2-1.4 4-1.4 6-.6v9c-2-.8-4-.8-6 .6-2-1.4-4-1.4-6-.6z"/><path d="M8 4.1v9"/>', () => openDoc(tut.id)))
  sheet.append(acts)

  let unread = 0
  try { unread = inboxUnread || 0 } catch (e) {} // boot: inbox.js has not run yet
  if (unread) {
    const note = el('button', 'wNote'); note.type = 'button'
    note.append(el('span', 'wDot'), el('span', null, t(unread === 1 ? 'docs.wInboxOne' : 'docs.wInboxMany', { n: unread })))
    note.onclick = e => { e.stopPropagation(); $('inboxBtn').click() }
    sheet.append(note)
  }

  const contents = el('section', 'wContents'); contents.setAttribute('aria-labelledby', 'wContentsH')
  const ch = el('h2', null, t('docs.wRecent')); ch.id = 'wContentsH'
  contents.append(ch)
  const docs = welcomeRecent()
  if (docs.length) { const list = el('div', 'wList'); docs.forEach(x => list.append(welcomeLine(x))); contents.append(list) }
  else contents.append(el('p', 'wEmpty', t('docs.wEmpty')))
  sheet.append(contents)

  const keys = el('p', 'wKeys')
  for (const [id, label] of [['openHelp', 'docs.wKeysHelp'], ['find', 'docs.wKeysFind']]) {
    const k = window.scKeyLabel ? window.scKeyLabel(id) : ''
    if (!k) continue
    if (keys.childNodes.length) keys.append(document.createTextNode('  ·  '))
    keys.append(el('kbd', null, k), document.createTextNode(' ' + t(label)))
  }
  if (keys.childNodes.length) sheet.append(keys)
  box.replaceChildren(sheet)
}
