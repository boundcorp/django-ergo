// Screenshots of the real Ergonaut chat screen (the Vite dev server, unmodified) with a mocked API: every /api request
// is answered by Playwright from the fixtures below, so nothing needs a backend. Output: ../../packages/ergo-chat-ui/fixtures/screenshots/ergonaut-*.png
//   node scripts/chat-screenshots.mjs        (needs playwright-core and a Chromium: CHROMIUM_PATH=/path/to/chrome)
// Set PLAYWRIGHT_CORE=/path/to/node_modules/playwright-core when it is not resolvable from here.
import { spawn } from 'node:child_process'
import { mkdirSync } from 'node:fs'
import { deflateSync } from 'node:zlib'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const out = resolve(here, '../../../packages/ergo-chat-ui/fixtures/screenshots')
mkdirSync(out, { recursive: true })
const { chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT_CORE || 'playwright-core')

const port = 3600 + Math.floor(Math.random() * 300)
const server = spawn('npx', ['vite', '--port', String(port), '--host', '127.0.0.1'], {
  cwd: resolve(here, '..'),
  stdio: ['ignore', 'pipe', 'pipe'],
  detached: true,
})
let log = ''
server.stdout.on('data', d => (log += d))
server.stderr.on('data', d => (log += d))
for (let i = 0; ; i++) {
  try {
    if ((await fetch(`http://127.0.0.1:${port}/`)).ok) break
  } catch {}
  if (i > 120) throw new Error('vite did not start:\n' + log)
  await new Promise(r => setTimeout(r, 500))
}

// --- fixtures -------------------------------------------------------------------------------------------------------
const T = (m, s = 0) => `2026-11-25T10:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}Z`
const SID = 'aaaaaaaa-1111-2222-3333-444444444444'
const OTHER = 'bbbbbbbb-1111-2222-3333-444444444444'
const text = t => ({ type: 'text', text: t })
const use = (id, name, input) => ({ type: 'tool_use', id, name, input })
const result = (id, content, is_error = false) => ({ type: 'tool_result', tool_use_id: id, content, is_error })
const m = (line, role, blocks, minute, extra = {}) => ({ line, role, blocks, timestamp: T(minute), ...extra })
const base = {
  debug_shortcut: false,
  session: {
    id: SID,
    bot: 'helper',
    title: 'Ship the billing export',
    role: 'thread',
    parent_id: null,
    status: 'active',
    username: 'lee',
    created_at: T(0),
    updated_at: T(30),
    model: '',
  },
  workers: [
    {
      id: 'w1',
      title: 'Run the export tests',
      function: 'orca',
      status: 'running',
      progress: '14 of 31 passed',
      result: null,
      error: '',
      created_at: T(5),
      completed_at: null,
    },
  ],
  sent: [],
  compactions: [],
  requests: [],
  todos: [],
  wakes: [],
  recurring_wakes: [],
  inbox: [],
  has_more: false,
  first_line: 0,
  message_count: 12,
}
const calls = [
  {
    id: 'call-1',
    kind: 'chat_reply',
    status: 'completed',
    request: '',
    response: { type: 'answer', text: 'Done', suggestions: ['Open the PR', 'Run it again'] },
    error: '',
    first_sequence: 0,
    last_sequence: 11,
    model_name: 'claude-sonnet-5-5',
    input_tokens: 4210,
    output_tokens: 812,
    turns_used: 5,
    pending_approvals: [],
    tools: [],
    created_at: T(1),
  },
]
const readOnly = Array.from({ length: 7 }, (_, i) =>
  use(`r${i}`, i % 2 ? 'files_read' : 'files_grep', { path: `src/billing/export_${i}.py` }),
)
const messages = [
  m(
    0,
    'user',
    [text('Can you finish the billing export? The CSV needs the tax column and the **tests** should pass.')],
    1,
    { author: { kind: 'django_user', ref: 'lee', display_name: 'Lee' } },
  ),
  m(
    1,
    'assistant',
    [
      text("On it. I'll read the export module first, then start a worker for the tests."),
      ...readOnly,
      use('e1', 'files_write', { path: 'src/billing/missing.py' }),
    ],
    2,
  ),
  m(
    2,
    'user',
    [
      ...readOnly.map((u, i) => result(u.id, `ok ${i}`)),
      result('e1', 'FileNotFoundError: src/billing/missing.py', true),
    ],
    2,
  ),
  m(
    3,
    'assistant',
    [
      use('t1', 'ergo_thread_send', {
        thread: 'new',
        bot: 'reviewer',
        message: 'Review the tax column change in billing/export.py before I merge.',
      }),
      use('w1', 'ergo_agent_start', { title: 'Run the export tests' }),
    ],
    3,
  ),
  m(
    4,
    'user',
    [
      result(
        't1',
        JSON.stringify({ thread_id: OTHER, title: 'Review: tax column', status: 'queued', bot: 'reviewer' }),
      ),
      result('w1', JSON.stringify({ id: 'w1', title: 'Run the export tests', status: 'running' })),
    ],
    3,
  ),
  m(5, 'user', [text('Build finished: 31 tests, 0 failures.')], 8, {
    author: { kind: 'system', ref: 'ci', display_name: 'CI' },
  }),
  m(
    6,
    'assistant',
    [
      text('The tax column is in and the suite passes. A chart of the totals:'),
      { type: 'attachment', label: 'totals.png', id: 'img1', kind: 'image', media_type: 'image/png' },
      { type: 'attachment', label: 'export.csv', id: 'f2', kind: 'file', media_type: 'text/csv' },
    ],
    9,
  ),
  m(
    7,
    'assistant',
    [
      use('rep', 'send_reply', {
        type: 'answer',
        text: 'All set. The export now has a `tax` column and 31 tests pass.\n\n```python\nrow = [order.id, order.total, order.tax]\n```\n\nWant me to open the PR?',
        suggestions: ['Open the PR', 'Run it again'],
      }),
    ],
    10,
  ),
]
const sessions = [
  { ...base.session },
  {
    ...base.session,
    id: OTHER,
    bot: 'reviewer',
    title: 'Review: tax column',
    started_by: 'Ship the billing export',
    started_by_id: SID,
  },
]
const bots = [
  {
    name: 'helper',
    description: 'Does the work',
    orchestration: true,
    knowledge: false,
    parent: '',
    root_session_id: null,
  },
  {
    name: 'reviewer',
    description: 'Reviews',
    orchestration: false,
    knowledge: false,
    parent: '',
    root_session_id: null,
  },
]
const models = {
  default: 'claude-sonnet-5-5',
  default_engine_type: 'claude',
  models: [{ id: 'claude-sonnet-5-5', name: 'claude-sonnet-5-5', label: 'Sonnet 5.5', engine_type: 'claude' }],
}
// A small valid PNG (a gradient), so thumbnails and the gallery really render.
function makePng(width, height) {
  const crcTable = Array.from({ length: 256 }, (_, n) => {
    let c = n
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    return c >>> 0
  })
  const crc = buf => {
    let c = 0xffffffff
    for (const byte of buf) c = crcTable[(c ^ byte) & 0xff] ^ (c >>> 8)
    return (c ^ 0xffffffff) >>> 0
  }
  const chunk = (type, data) => {
    const body = Buffer.concat([Buffer.from(type), data])
    const head = Buffer.alloc(4)
    head.writeUInt32BE(data.length)
    const tail = Buffer.alloc(4)
    tail.writeUInt32BE(crc(body))
    return Buffer.concat([head, body, tail])
  }
  const header = Buffer.alloc(13)
  header.writeUInt32BE(width, 0)
  header.writeUInt32BE(height, 4)
  header.set([8, 2, 0, 0, 0], 8)
  const rows = []
  for (let y = 0; y < height; y++) {
    const row = Buffer.alloc(1 + width * 3)
    for (let x = 0; x < width; x++)
      row.set([40 + (x * 180) / width, 90 + (y * 120) / height, 200 - (x * 100) / width], 1 + x * 3)
    rows.push(row)
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', header),
    chunk('IDAT', deflateSync(Buffer.concat(rows))),
    chunk('IEND', Buffer.alloc(0)),
  ])
}
const png = makePng(320, 120)

const scenarios = {
  chat: { detail: { ...base, messages, calls }, draft: '', attach: true },
  running: {
    detail: {
      ...base,
      messages: messages.slice(0, 5),
      calls: [{ ...calls[0], status: 'in_progress', last_sequence: null, response: null }],
    },
    draft: 'Use the staging fixtures instead, please',
    attach: false,
  },
  approvals: {
    detail: {
      ...base,
      workers: [],
      messages: [
        messages[0],
        m(
          1,
          'assistant',
          [
            text('Two commands need your approval before I continue.'),
            use('a1', 'shell_run', { cmd: 'pytest tests/billing -q' }),
            use('a2', 'files_write', { path: 'src/billing/export.py' }),
          ],
          2,
        ),
      ],
      calls: [
        {
          ...calls[0],
          status: 'awaiting_approval',
          last_sequence: null,
          response: null,
          pending_approvals: [
            { id: 'a1', name: 'shell_run', input: { cmd: 'pytest tests/billing -q' } },
            {
              id: 'a2',
              name: 'files_write',
              input: { path: 'src/billing/export.py', content: 'row = [order.id, order.total, order.tax]' },
              preview: '+ row = [order.id, order.total, order.tax]\n- row = [order.id, order.total]',
            },
          ],
        },
      ],
    },
    draft: '',
    attach: false,
  },
}

// --- mocked API -----------------------------------------------------------------------------------------------------
function handler(scenario) {
  return async route => {
    const url = new URL(route.request().url())
    const path = url.pathname.replace(/^\/api/, '')
    const json = (body, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    if (path === '/auth/csrf') return json({ csrftoken: 'x' })
    if (path === '/auth/me')
      return json({ id: 'u1', username: 'lee', email: 'lee@example.com', first_name: 'Lee', last_name: '' })
    if (path === '/bots') return json(bots)
    if (path === '/sessions') return json(sessions)
    if (path === `/sessions/${SID}`) return json(scenario.detail)
    if (path === `/sessions/${SID}/events`)
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: ': keep-alive\n\n' })
    if (path === `/sessions/${SID}/attachments`) return json([])
    if (path.endsWith('/models')) return json(models)
    if (path.startsWith('/attachments/'))
      return route.fulfill({ status: 200, contentType: path.includes('f2') ? 'text/csv' : 'image/png', body: png })
    if (path === '/pins' || path === '/workers/active' || path.endsWith('/pins'))
      return json(path.endsWith('/pins') ? [] : path === '/pins' ? {} : [])
    if (path === '/realtime-voice/config') return json({ enabled: false })
    if (path === '/realtime-voice/sessions') return json([])
    if (path === '/bots/errors') return json([])
    if (path === '/version') return json({ version: 'dev', commit: 'dev' })
    console.log('  (unmocked)', route.request().method(), path)
    return json({ detail: 'not mocked' }, 404)
  }
}

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined })
const problems = []
async function shoot(name, scenario, viewport, { typeDraft = true, attachFile = false, scrollTo = null } = {}) {
  const context = await browser.newContext({ viewport, deviceScaleFactor: 1 })
  const page = await context.newPage()
  page.on('pageerror', e => problems.push(`${name}: pageerror ${e.message}`))
  page.on(
    'console',
    msg =>
      msg.type() === 'error' &&
      !/Failed to load resource|EventSource/.test(msg.text()) &&
      problems.push(`${name}: console ${msg.text()}`),
  )
  await page.route('**/api/**', handler(scenario))
  await page.goto(`http://127.0.0.1:${port}/s/${SID}`)
  await page.waitForSelector('.chat-composer textarea')
  await page.waitForTimeout(600)
  if (attachFile) {
    await page.route('**/api/sessions/*/attachments', async route =>
      route.request().method() === 'POST'
        ? route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({
              id: 'up1',
              filename: 'invoice-march.png',
              kind: 'image',
              view: 'image',
              media_type: 'image/png',
            }),
          })
        : route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }),
    )
    await page.setInputFiles('.chat-composer input[type=file]', {
      name: 'invoice-march.png',
      mimeType: 'image/png',
      buffer: png,
    })
    await page.waitForSelector('.chat-composer img')
  }
  if (typeDraft && scenario.draft) await page.fill('.chat-composer textarea', scenario.draft)
  if (scrollTo) {
    await page.evaluate(sel => {
      const box = document.querySelector('.chat-transcript')
      const el = document.querySelector(sel)
      box.scrollTop += el.getBoundingClientRect().top - box.getBoundingClientRect().top - 8
    }, scrollTo)
  }
  await page.waitForTimeout(300)
  // No horizontal overflow at the page or the chat column; nothing clipped under the composer.
  const overflow = await page.evaluate(() => {
    const t = document.querySelector('.chat-transcript')
    return {
      page: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      transcript: t ? t.scrollWidth - t.clientWidth : 0,
    }
  })
  if (overflow.page > 0 || overflow.transcript > 0)
    problems.push(`${name}: horizontal overflow ${JSON.stringify(overflow)}`)
  await page.screenshot({ path: `${out}/${name}.png` })
  console.log(`  shot ${name} (${viewport.width}x${viewport.height})`)
  await context.close()
}
const desktop = { width: 1280, height: 800 }
const mobile = { width: 390, height: 844 }
try {
  await shoot('ergonaut-desktop-chat', scenarios.chat, desktop, { attachFile: true })
  await shoot('ergonaut-mobile-chat', scenarios.chat, mobile, { attachFile: true })
  await shoot('ergonaut-desktop-cards', scenarios.chat, desktop, { scrollTo: '#m-1' })
  await shoot('ergonaut-mobile-cards', scenarios.chat, mobile, { scrollTo: '#m-1' })
  await shoot('ergonaut-desktop-running', scenarios.running, desktop)
  await shoot('ergonaut-mobile-running', scenarios.running, mobile)
  await shoot('ergonaut-desktop-approvals', scenarios.approvals, desktop)
  await shoot('ergonaut-mobile-approvals', scenarios.approvals, mobile)
} finally {
  await browser.close()
  process.kill(-server.pid)
}
if (problems.length) {
  console.log('PROBLEMS:\n' + problems.join('\n'))
  process.exit(1)
}
console.log('screenshots OK')
