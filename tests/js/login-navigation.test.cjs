const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')

const source = fs.readFileSync('web/js/96-login-actions.js', 'utf8')
const start = source.indexOf('let tokenInvalid = false') // bootSession + the /api/me boot block
const restore = source.slice(start, source.indexOf('try { (window.requestIdleCallback', start))

test('late session initialization respects an already-started document navigation', async () => {
  for (const openN of [0, 1]) {
    const opened = []
    const context = vm.createContext({
      api: async () => ({ user: 'alice' }), loadSpell() {}, paintMe() {},
      $: () => ({ style: {}, classList: { add() {} } }), cm: { measure() {} },
      openN, docId: '', tplName: '', URLSearchParams, location: { search: '' },
      sidebar: async () => {}, watchSidebar() {}, loadInbox: async () => {}, loadTpl: async () => {}, checkJoin: async () => false,
      lastOwn: [{ id: 'tutorial', title: 'Tutorial' }], lastShared: [], openDoc: id => opened.push(id),
      console,
    })
    await vm.runInContext(restore, context)
    assert.deepEqual(opened, openN === 0 ? ['tutorial'] : [])
  }
})

const oidcStart = source.indexOf('function oidcErrMsg(code)')
const oidcEnd = source.indexOf('const hadOidcErr = checkOidcError()')
const oidcHelpers = source.slice(oidcStart, oidcEnd)

test('OIDC login config hides password fields and auto-redirects with join token', async () => {
  const nodes = {
    err: { textContent: '' },
    oidcBtn: { textContent: '', href: '' },
    oidcBox: { style: { display: 'none' }, classList: { toggle(c, v) { this[c] = v } } },
    pwLoginFields: { style: { display: '' } },
    oidcSep: { style: { display: '' } },
    mode: { style: { display: '' } },
  }
  const assigned = []
  let replaced = ''
  const context = vm.createContext({
    $: id => nodes[id],
    t: (k, p) => (p && p.provider ? `${k}:${p.provider}` : k),
    URLSearchParams,
    regMode: false,
    console,
    history: { replaceState(_s, _t, url) { replaced = url } },
    location: {
      pathname: '/',
      search: '?oidc_error=access_denied&join=tok42',
      assign: u => assigned.push(u),
    },
    api: async () => ({
      oidc: { enabled: true, provider_name: 'Authentik', auto_redirect: true },
      password_login: false,
      registration: 'closed',
    }),
  })
  vm.runInContext(oidcHelpers, context)
  assert.equal(context.checkOidcError(), true)
  assert.equal(replaced, '/?join=tok42')
  assert.equal(nodes.err.textContent, 'login.oidcAccessDenied')

  // Suppresses auto-redirect when hadOidcErr=true, but configures button & hides password form.
  context.location.search = '?join=tok42'
  await context.loadAuthConfig(true, false)
  assert.equal(nodes.oidcBox.style.display, '')
  assert.equal(nodes.oidcBox.classList.only, true)
  assert.equal(nodes.pwLoginFields.style.display, 'none')
  assert.equal(nodes.oidcSep.style.display, 'none')
  assert.equal(nodes.oidcBtn.textContent, 'login.signInWith:Authentik')
  assert.equal(nodes.oidcBtn.href, '/api/oidc/login?join=tok42')
  assert.deepEqual(assigned, [])

  assert.equal(context.oidcErrMsg('rate_limited'), 'login.oidcRateLimited')

  // Auto-redirects when no error or invite is present.
  await context.loadAuthConfig(false, false)
  assert.deepEqual(assigned, ['/api/oidc/login?join=tok42'])
})

test('OIDC auto-redirect is suppressed once after sign-out and closed registration resets invite state', async () => {
  const store = { typst_logged_out: '1' }
  const sessionStorage = {
    getItem: k => (Object.hasOwn(store, k) ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v) },
    removeItem: k => { delete store[k] },
  }
  let modeClicks = 0
  const nodes = {
    err: { textContent: '' },
    oidcBtn: { textContent: '', href: '' },
    oidcBox: { style: { display: 'none' }, classList: { toggle(c, v) { this[c] = v } } },
    pwLoginFields: { style: { display: '' } },
    oidcSep: { style: { display: '' } },
    inv: { value: 'INVITE123' },
    mode: {
      style: { display: '' },
      click() {
        modeClicks++
        context.regMode = !context.regMode
      },
    },
  }
  const assigned = []
  let authCfg = {
    oidc: { enabled: true, provider_name: 'Kanidm', auto_redirect: true },
    password_login: true,
    registration: 'closed',
  }
  const context = vm.createContext({
    $: id => nodes[id],
    t: (k, p) => (p && p.provider ? `${k}:${p.provider}` : k),
    URLSearchParams,
    regMode: true,
    console,
    sessionStorage,
    history: { replaceState() {} },
    location: { pathname: '/', search: '', assign: u => assigned.push(u) },
    api: async () => authCfg,
  })
  vm.runInContext(oidcHelpers, context)

  // Sign-out suppresses auto-redirect once and clears the flag; closed registration resets invite mode.
  await context.loadAuthConfig(false, false)
  assert.deepEqual(assigned, [])
  assert.equal(store.typst_logged_out, undefined)
  assert.equal(modeClicks, 1)
  assert.equal(context.regMode, false)
  assert.equal(nodes.inv.value, '')
  assert.equal(nodes.mode.style.display, 'none')

  // Next reload without sign-out flag auto-redirects.
  await context.loadAuthConfig(false, false)
  assert.deepEqual(assigned, ['/api/oidc/login'])
})
