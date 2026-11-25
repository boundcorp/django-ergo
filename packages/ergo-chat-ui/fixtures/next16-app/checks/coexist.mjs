// Coexistence checks for @ergo/chat-ui inside a Next 16 + MUI 6 + Emotion app. Run from the built fixture:
//   node checks/coexist.mjs [--dev] [--screenshots <dir>]
// Needs a Chromium: playwright's own (`npx playwright install chromium`) or CHROMIUM_PATH=/path/to/chrome.
import { spawn } from 'node:child_process'
import { mkdirSync } from 'node:fs'
import { createRequire } from 'node:module'
import assert from 'node:assert/strict'

const require = createRequire(import.meta.url)
const { chromium } = require('playwright-core')
const args = process.argv.slice(2)
const dev = args.includes('--dev')
const shotDir = args.includes('--screenshots') ? args[args.indexOf('--screenshots') + 1] : null
const port = 3100 + Math.floor(Math.random() * 500)
const url = `http://127.0.0.1:${port}/`

const server = spawn(
  'npx',
  dev ? ['next', 'dev', '--webpack', '-p', String(port)] : ['next', 'start', '-p', String(port)],
  { env: { ...process.env, NEXT_TELEMETRY_DISABLED: '1' }, stdio: ['ignore', 'pipe', 'pipe'], detached: true },
)
let serverLog = ''
server.stdout.on('data', d => (serverLog += d))
server.stderr.on('data', d => (serverLog += d))
for (let i = 0; ; i++) {
  try {
    if ((await fetch(url)).ok) break
  } catch {}
  if (i > 120) throw new Error('server did not start:\n' + serverLog)
  await new Promise(r => setTimeout(r, 500))
}

const results = []
const ok = (name, detail = '') => {
  results.push(name)
  console.log(`  ok  ${name}${detail ? ' (' + detail + ')' : ''}`)
}
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined })
let failed = null

// Computed styles of host (MUI) elements. Compared with and without the package stylesheet.
const SNAPSHOT_PROPS = [
  'font-family',
  'font-size',
  'font-weight',
  'line-height',
  'color',
  'background-color',
  'margin',
  'padding',
  'box-sizing',
  'border-radius',
  'border-top-width',
  'text-transform',
  'letter-spacing',
  'display',
  'position',
  'z-index',
  'width',
  'height',
]
const snapshot = page =>
  page.evaluate(props => {
    const out = {}
    const sel = {
      html: 'html',
      body: 'body',
      appbar: '.MuiAppBar-root',
      'appbar-btn': '[data-testid=menu-open]',
      title: '.MuiTypography-h6',
      'perm-drawer': '.MuiDrawer-docked .MuiDrawer-paper',
      'list-btn': '.MuiListItemButton-root',
      toolbar: '.MuiToolbar-root',
      'chat-host': '[data-testid=chat-host]',
    }
    for (const [k, s] of Object.entries(sel)) {
      const el = document.querySelector(s)
      if (!el) {
        out[k] = null
        continue
      }
      const cs = getComputedStyle(el)
      out[k] = Object.fromEntries(props.map(p => [p, cs.getPropertyValue(p)]))
    }
    return out
  }, SNAPSHOT_PROPS)

async function openPage(viewport, { stripPackageCss = false } = {}) {
  const context = await browser.newContext({ viewport })
  const page = await context.newPage()
  const logs = []
  // The dev server's HMR websocket is not served through this harness; ignore only that.
  const noise = t => dev && t.includes('/_next/hmr')
  page.on(
    'console',
    m => ['error', 'warning'].includes(m.type()) && !noise(m.text()) && logs.push(`${m.type()}: ${m.text()}`),
  )
  page.on('pageerror', e => logs.push(`pageerror: ${e.message}`))
  if (stripPackageCss) {
    await page.route('**/*.css*', async route => {
      const res = await route.fetch()
      const body = await res.text()
      if (body.includes('--ergo-chat-fg')) return route.fulfill({ response: res, body: '' })
      return route.fulfill({ response: res, body })
    })
  }
  await page.goto(url, { waitUntil: 'networkidle' })
  await page.waitForSelector('.ergo-chat')
  await page.waitForFunction(() => document.fonts.status === 'loaded')
  return { page, logs, context }
}

try {
  const DESKTOP = { width: 1280, height: 800 }
  const MOBILE = { width: 390, height: 844 }

  // 1. Hydration and console: nothing at warning or error level, in both viewports.
  for (const [name, vp] of [
    ['desktop', DESKTOP],
    ['mobile', MOBILE],
  ]) {
    const { page, logs, context } = await openPage(vp)
    // Interact so late hydration problems surface too.
    await page.fill('textarea', 'hello from the fixture')
    await page.keyboard.press('Enter')
    await page.waitForSelector('text=hello from the fixture')
    await page.click('[data-testid=dialog-open]')
    await page.waitForSelector('.MuiDialog-paper')
    await page.keyboard.press('Escape')
    assert.deepEqual(logs, [], `console errors/warnings on ${name}:\n${logs.join('\n')}`)
    ok(`no console errors or hydration warnings (${name}${dev ? ', next dev' : ', next start'})`)
    await context.close()
  }

  // 2. The package stylesheet does not change host (MUI, html, body) styles. (Production only: next dev injects CSS
  // through JS, so the stylesheet cannot be blocked at the network.)
  if (!dev) {
    const withCss = await openPage(DESKTOP)
    const without = await openPage(DESKTOP, { stripPackageCss: true })
    assert.equal(
      await without.page.evaluate(() =>
        getComputedStyle(document.querySelector('.ergo-chat')).getPropertyValue('--ergo-chat-fg'),
      ),
      '',
      'package css was not stripped',
    )
    const a = await snapshot(withCss.page)
    const b = await snapshot(without.page)
    for (const k of Object.keys(a)) assert.ok(a[k], `missing host element ${k}`)
    // The chat host box is sized by the host, not by the package: compare everything except the chat's contents.
    assert.deepEqual(a, b, 'package CSS changed host element styles')
    // Overlays too: temporary Drawer and Dialog.
    for (const { page } of [withCss, without]) {
      await page.click('[data-testid=dialog-open]')
      await page.waitForSelector('.MuiDialog-paper')
    }
    const dialogStyle = p =>
      p.evaluate(props => {
        const cs = getComputedStyle(document.querySelector('.MuiDialog-paper'))
        return Object.fromEntries(props.map(x => [x, cs.getPropertyValue(x)]))
      }, SNAPSHOT_PROPS)
    assert.deepEqual(
      await dialogStyle(withCss.page),
      await dialogStyle(without.page),
      'package CSS changed Dialog styles',
    )
    const font = await withCss.page.evaluate(() => getComputedStyle(document.body).fontFamily)
    assert.match(font, /Roboto/, 'body font must stay the MUI theme font')
    ok(
      'package CSS leaves html/body/font and MUI AppBar, Button, Typography, Drawer, Dialog styles unchanged',
      `${Object.keys(a).length} elements x ${SNAPSHOT_PROPS.length} props, body font ${font.split(',')[0]}`,
    )
    await withCss.context.close()
    await without.context.close()
  }

  // 3. CssBaseline does not break the chat layout; the chat inherits the MUI font; overlays stack above it.
  for (const [name, vp] of [
    ['desktop', DESKTOP],
    ['mobile', MOBILE],
  ]) {
    const { page, context } = await openPage(vp)
    const m = await page.evaluate(() => {
      const r = s => document.querySelector(s)
      const box = el => el.getBoundingClientRect()
      const root = r('.ergo-chat'),
        host = r('[data-testid=chat-host]')
      const transcript = r('.ergo-chat__transcript') || r('.ergo-chat [role=log]')
      const composer = r('.ergo-chat__composer'),
        textarea = r('.ergo-chat textarea')
      const btn = r('.ergo-chat__btn--primary')
      const tool = r('.ergo-chat__tool')
      const cs = el => getComputedStyle(el)
      return {
        hostH: host.clientHeight,
        rootH: box(root).height,
        hostW: host.clientWidth,
        rootW: box(root).width,
        hostBottom: box(host).bottom,
        composerBottom: box(composer).bottom,
        composerTop: box(composer).top,
        transcriptBottom: box(transcript).bottom,
        taRight: box(textarea).right,
        rootRight: box(root).right,
        transcriptOverflowY: cs(transcript).overflowY,
        tScroll: transcript.scrollHeight,
        tClient: transcript.clientHeight,
        docOverflowX: document.documentElement.scrollWidth - window.innerWidth,
        rootOverflowX: root.scrollWidth - root.clientWidth,
        rootFont: cs(root).fontFamily,
        btnFont: cs(btn).fontFamily,
        taFont: cs(textarea).fontFamily,
        toolFont: cs(tool).fontFamily,
        taBox: cs(textarea).boxSizing,
        btnBox: cs(btn).boxSizing,
        bodyFont: cs(document.body).fontFamily,
        margin: cs(document.body).margin,
        rootColor: cs(root).color,
      }
    })
    assert.ok(
      Math.abs(m.rootH - m.hostH) <= 1 && Math.abs(m.rootW - m.hostW) <= 1,
      `chat fills host (${name}): ${m.rootW}x${m.rootH} vs ${m.hostW}x${m.hostH}`,
    )
    assert.ok(m.composerBottom <= m.hostBottom + 1 && m.composerTop > 0, 'composer inside host, visible')
    assert.ok(m.transcriptBottom <= m.composerTop + 1, 'transcript sits above the composer')
    assert.ok(['auto', 'scroll'].includes(m.transcriptOverflowY), 'transcript is a scroll container')
    assert.ok(m.docOverflowX <= 0 && m.rootOverflowX <= 0, `no horizontal overflow (${name})`)
    assert.ok(m.taRight <= m.rootRight + 0.5, 'textarea stays inside the chat')
    assert.equal(m.rootFont, m.bodyFont, 'chat inherits the host font')
    assert.equal(m.btnFont, m.bodyFont, 'chat buttons inherit the host font')
    assert.equal(m.taFont, m.bodyFont, 'composer textarea inherits the host font')
    assert.equal(m.taBox, 'border-box')
    assert.equal(m.btnBox, 'border-box')
    assert.notEqual(m.rootColor, '', 'chat has its own colour')
    // A short viewport forces overflow: the transcript scrolls and the composer stays on screen.
    await page.setViewportSize({ width: vp.width, height: 480 })
    const short = await page.evaluate(() => {
      const t = document.querySelector('[role=log]'),
        c = document.querySelector('.ergo-chat__composer')
      t.scrollTop = 0
      t.scrollTop = 50
      return {
        overflows: t.scrollHeight > t.clientHeight,
        scrolled: t.scrollTop > 0,
        composerBottom: c.getBoundingClientRect().bottom,
        page: document.documentElement.scrollHeight - innerHeight,
      }
    })
    assert.ok(short.overflows && short.scrolled, 'transcript scrolls in a short viewport')
    assert.ok(
      short.composerBottom <= 480 && short.page <= 0,
      `composer stays on screen, page does not scroll: ${JSON.stringify(short)}`,
    )
    await page.setViewportSize(vp)
    // z-order: Dialog and temporary Drawer render above the chat (hit-test at their centres), AppBar above the page.
    await page.click('[data-testid=dialog-open]')
    await page.waitForSelector('.MuiDialog-paper')
    await page.waitForTimeout(600) // let the transition finish
    const dlg = await page.evaluate(() => {
      const p = document.querySelector('.MuiDialog-paper').getBoundingClientRect()
      const hit = document.elementFromPoint(p.x + p.width / 2, p.y + p.height / 2)
      return { insideDialog: !!hit?.closest('.MuiDialog-root'), insideChat: !!hit?.closest('.ergo-chat') }
    })
    assert.ok(dlg.insideDialog && !dlg.insideChat, 'Dialog above chat')
    await page.keyboard.press('Escape')
    await page.waitForSelector('.MuiDialog-paper', { state: 'detached' })
    await page.click('[data-testid=menu-open]')
    await page.waitForSelector('[data-testid=temp-drawer] .MuiDrawer-paper')
    await page.waitForTimeout(600)
    const drw = await page.evaluate(() => {
      const p = document.querySelector('[data-testid=temp-drawer] .MuiDrawer-paper').getBoundingClientRect()
      const hit = document.elementFromPoint(p.x + 10, p.y + 100)
      return { insideDrawer: !!hit?.closest('.MuiDrawer-root'), insideChat: !!hit?.closest('.ergo-chat') }
    })
    assert.ok(drw.insideDrawer && !drw.insideChat, 'temporary Drawer above chat')
    await page.keyboard.press('Escape')
    await page.waitForSelector('[data-testid=temp-drawer] .MuiDrawer-paper', { state: 'detached' })
    const appbar = await page.evaluate(() => {
      const p = document.querySelector('.MuiAppBar-root').getBoundingClientRect()
      const hit = document.elementFromPoint(p.x + p.width / 2, p.y + p.height / 2)
      return !!hit?.closest('.MuiAppBar-root') && !hit?.closest('.ergo-chat')
    })
    assert.ok(appbar, 'AppBar above chat')
    ok(
      `CssBaseline + MUI layout: chat fills host, scrolls, composer visible, no overflow, inherits font, overlays above (${name})`,
      `${Math.round(m.rootW)}x${Math.round(m.rootH)}`,
    )
    await context.close()
  }

  // 4. Theming through CSS variables, scoped to a host selector.
  {
    const { page, context } = await openPage(DESKTOP)
    const readTokens = () =>
      page.evaluate(() => {
        const cs = getComputedStyle(document.querySelector('.ergo-chat__btn--primary'))
        const user = document.querySelector('.ergo-chat__msg--user .ergo-chat__bubble')
        return {
          bg: cs.backgroundColor,
          radius: cs.borderRadius,
          userBg: user ? getComputedStyle(user).backgroundColor : null,
        }
      })
    const before = await readTokens()
    await page.addStyleTag({
      content:
        '.host-root .ergo-chat { --ergo-chat-accent: rgb(200, 10, 20); --ergo-chat-radius: 0px; --ergo-chat-user-bg: rgb(1, 2, 3); }',
    })
    const after = await readTokens()
    assert.notEqual(before.bg, after.bg)
    assert.equal(after.bg, 'rgb(200, 10, 20)', 'accent token themes the primary button')
    assert.notEqual(before.radius, after.radius)
    ok('package tokens are themeable with CSS variables', `accent ${before.bg} -> ${after.bg}`)
    await context.close()
  }

  // 5. Screenshots.
  if (shotDir) {
    mkdirSync(shotDir, { recursive: true })
    for (const [name, vp] of [
      ['desktop', DESKTOP],
      ['mobile', MOBILE],
    ]) {
      const { page, context } = await openPage(vp)
      await page.screenshot({ path: `${shotDir}/next16-mui-${name}.png` })
      if (name === 'desktop') {
        await page.click('[data-testid=dialog-open]')
        await page.waitForSelector('.MuiDialog-paper')
        await page.waitForTimeout(400)
        await page.screenshot({ path: `${shotDir}/next16-mui-${name}-dialog.png` })
      }
      await context.close()
    }
    ok('screenshots written', shotDir)
  }
} catch (e) {
  failed = e
} finally {
  await browser.close()
  try {
    process.kill(-server.pid, 'SIGTERM') // npx and the next server it spawned
  } catch {}
}
if (failed) {
  console.error('FAIL:', failed.message)
  process.exit(1)
}
console.log(`coexistence OK (${results.length} groups)`)
process.exit(0)
