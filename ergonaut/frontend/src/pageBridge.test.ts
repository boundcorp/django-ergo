import { describe, expect, test } from 'bun:test'
import type { PageActionBody, PageActionResponse } from './api'
import {
  PageBridge,
  RERENDER_DELAY_MS,
  SCROLL_TIMEOUT_MS,
  eventsUrl,
  ergoMessage,
  parseStreamEvent,
} from './pageBridge'
import type { ViewerMessage } from './pageBridge'

const FRAME = { name: 'the page iframe' }
const OTHER = { name: 'some other window' }

// Timers that only move when the test says so.
function fakeTimers() {
  let now = 0
  let next = 1
  const pending = new Map<number, { at: number; fn: () => void }>()
  return {
    setTimeout: (fn: () => void, ms: number) => {
      const id = next++
      pending.set(id, { at: now + ms, fn })
      return id
    },
    clearTimeout: (handle: unknown) => {
      pending.delete(handle as number)
    },
    advance(ms: number) {
      const end = now + ms
      for (;;) {
        const due = [...pending.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0]
        if (!due) break
        pending.delete(due[0])
        now = due[1].at
        due[1].fn()
      }
      now = end
    },
  }
}

type Calls = { bot: string; name: string; body: PageActionBody }

function setup(
  options: {
    respond?: (call: Calls, n: number) => PageActionResponse | Error
    approve?: boolean
  } = {},
) {
  const timers = fakeTimers()
  const posted: ViewerMessage[] = []
  const calls: Calls[] = []
  const confirms: string[] = []
  const toasts: [string, string][] = []
  const opened: string[] = []
  const streams: string[][] = []
  let reloads = 0
  const bridge = new PageBridge({
    bot: 'pantry',
    page: 'pages/stock.jhtml',
    sessionId: 'sess-1',
    frameWindow: () => FRAME,
    postToPage: m => posted.push(m),
    callAction: async (bot, name, body) => {
      const call = { bot, name, body }
      calls.push(call)
      const outcome = options.respond?.(call, calls.length) ?? { result: {} }
      if (outcome instanceof Error) throw outcome
      return outcome
    },
    confirm: async preview => {
      confirms.push(preview)
      return options.approve ?? true
    },
    notify: (text, kind) => toasts.push([text, kind]),
    openUrl: url => opened.push(url),
    reloadFrame: () => {
      reloads++
    },
    onStreamTables: tables => streams.push(tables),
    timers,
  })
  return {
    bridge,
    timers,
    posted,
    calls,
    confirms,
    toasts,
    opened,
    streams,
    reloads: () => reloads,
    send: (data: unknown, source: unknown = FRAME) => bridge.handleMessage(source, data),
    ready: (tables: string[], listening: string[] = []) =>
      bridge.handleMessage(FRAME, { type: 'ergo:ready', tables, listening }),
  }
}

describe('message validation', () => {
  test('ignores messages from any window but the page iframe', async () => {
    const t = setup()
    const call = { type: 'ergo:call', id: '1', name: 'restock', args: {} }
    expect(await t.send(call, OTHER)).toBe(false)
    expect(await t.send(call, null)).toBe(false)
    expect(await t.bridge.handleMessage(undefined, call)).toBe(false)
    expect(t.calls).toEqual([])
    expect(t.posted).toEqual([])
    expect(await t.send({ type: 'ergo:reload' }, OTHER)).toBe(false)
    expect(t.reloads()).toBe(0)
    expect(t.posted).toEqual([])
  })

  test('ignores messages while the iframe does not exist', async () => {
    const bridge = new PageBridge({
      bot: 'pantry',
      page: 'p',
      sessionId: null,
      frameWindow: () => null,
      postToPage: () => {
        throw new Error('nothing to post to')
      },
      callAction: async () => {
        throw new Error('no call expected')
      },
      confirm: async () => true,
      notify: () => {},
      openUrl: () => {},
      reloadFrame: () => {},
    })
    expect(await bridge.handleMessage(null, { type: 'ergo:call', id: '1', name: 'x', args: {} })).toBe(false)
  })

  test('ignores data that is not an ergo message', async () => {
    const t = setup()
    for (const data of [null, 'ergo:call', 42, [], {}, { type: 7 }, { type: 'call' }, { type: 'other:call' }]) {
      expect(await t.send(data)).toBe(false)
    }
    expect(await t.send({ type: 'ergo:unknown' })).toBe(false)
    expect(t.calls).toEqual([])
    expect(t.posted).toEqual([])
  })

  test('ergoMessage keeps the fields of a well-formed message', () => {
    expect(ergoMessage({ type: 'ergo:focus', focused: true })).toEqual({ type: 'ergo:focus', focused: true })
    expect(ergoMessage({ type: 'x' })).toBeNull()
  })
})

describe('page actions', () => {
  test('calls the pin bot with the pin page and session, and forwards the whole result', async () => {
    const result = { message: 'Added', rows: 2 }
    const t = setup({ respond: () => ({ result }) })
    await t.send({ type: 'ergo:call', id: 'a1', name: 'restock', args: { item: 'flour', qty: 2 } })
    expect(t.calls).toEqual([
      {
        bot: 'pantry',
        name: 'restock',
        body: { args: { item: 'flour', qty: 2 }, page: 'pages/stock.jhtml', session_id: 'sess-1' },
      },
    ])
    expect(t.posted).toEqual([{ type: 'ergo:result', id: 'a1', ok: true, result }])
    expect(t.toasts).toEqual([['Added', 'info']])
  })

  test('the page cannot choose the bot, page or session', async () => {
    const t = setup()
    await t.send({
      type: 'ergo:call',
      id: 'a1',
      name: 'restock',
      args: {},
      bot: 'other',
      page: 'elsewhere',
      session_id: 'x',
      approval: 'forged',
    })
    expect(t.calls).toEqual([
      { bot: 'pantry', name: 'restock', body: { args: {}, page: 'pages/stock.jhtml', session_id: 'sess-1' } },
    ])
  })

  test('an API error goes to the page and a toast', async () => {
    const t = setup({ respond: () => new Error('Unknown action') })
    await t.send({ type: 'ergo:call', id: 'a1', name: 'nope', args: {} })
    expect(t.posted).toEqual([{ type: 'ergo:result', id: 'a1', ok: false, error: 'Unknown action' }])
    expect(t.toasts).toEqual([['Unknown action', 'error']])
  })

  test('rejects arguments that are not an object', async () => {
    const t = setup()
    await t.send({ type: 'ergo:call', id: 'a1', name: 'x', args: [1] })
    expect(t.calls).toEqual([])
    expect(t.posted).toEqual([{ type: 'ergo:result', id: 'a1', ok: false, error: 'Arguments must be an object' }])
  })

  test('approval: Yes repeats the call with the token, which the page never sees', async () => {
    const token = 'signed-token-123'
    const t = setup({
      respond: (call, n) =>
        n === 1
          ? { needs_approval: true, preview: 'Delete 3 rows?', approval: token }
          : { result: { message: 'Deleted' } },
    })
    await t.send({ type: 'ergo:call', id: 'a1', name: 'purge', args: { n: 3 } })
    expect(t.confirms).toEqual(['Delete 3 rows?'])
    expect(t.calls).toHaveLength(2)
    expect(t.calls[0].body.approval).toBeUndefined()
    expect(t.calls[1].body).toEqual({
      args: { n: 3 },
      page: 'pages/stock.jhtml',
      session_id: 'sess-1',
      approval: token,
    })
    expect(t.posted).toEqual([{ type: 'ergo:result', id: 'a1', ok: true, result: { message: 'Deleted' } }])
    expect(JSON.stringify(t.posted)).not.toContain(token)
  })

  test('approval: No replies Cancelled and makes no second call', async () => {
    const token = 'signed-token-123'
    const t = setup({
      approve: false,
      respond: () => ({ needs_approval: true, preview: 'Delete 3 rows?', approval: token }),
    })
    await t.send({ type: 'ergo:call', id: 'a1', name: 'purge', args: {} })
    expect(t.calls).toHaveLength(1)
    expect(t.posted).toEqual([{ type: 'ergo:result', id: 'a1', ok: false, error: 'Cancelled' }])
    expect(JSON.stringify(t.posted)).not.toContain(token)
  })

  test('a second approval request after approving is an error, not another dialog', async () => {
    const t = setup({ respond: () => ({ needs_approval: true, preview: 'Sure?', approval: 'tok' }) })
    await t.send({ type: 'ergo:call', id: 'a1', name: 'purge', args: {} })
    expect(t.confirms).toHaveLength(1)
    expect(t.calls).toHaveLength(2)
    expect(t.posted).toEqual([{ type: 'ergo:result', id: 'a1', ok: false, error: 'The approval was not accepted' }])
    expect(JSON.stringify(t.posted)).not.toContain('tok')
  })

  test('opens only http and https URLs from a result', async () => {
    const urls = [
      'https://example.com/report',
      'http://example.com/a?b=1',
      'javascript:alert(1)',
      'data:text/html,<script>1</script>',
      'file:///etc/passwd',
      '/relative/path',
      'not a url',
    ]
    for (const open of [...urls, 42, null]) {
      const t = setup({ respond: () => ({ result: { open } }) })
      await t.send({ type: 'ergo:call', id: 'a', name: 'x', args: {} })
      expect(t.opened).toEqual(
        typeof open === 'string' && /^https?:\/\/example\.com/.test(open) ? [new URL(open).href] : [],
      )
    }
  })

  test('reload:true re-renders at once, without waiting for the debounce', async () => {
    const t = setup({ respond: () => ({ result: { reload: true } }) })
    await t.send({ type: 'ergo:call', id: 'a', name: 'x', args: {} })
    expect(t.posted.map(m => m.type)).toEqual(['ergo:result', 'ergo:getscroll'])
    t.timers.advance(SCROLL_TIMEOUT_MS)
    expect(t.reloads()).toBe(1)
  })
})

describe('live changes', () => {
  test('tables in listening go to the page, one message each, with no re-render', async () => {
    const t = setup()
    await t.ready(['A', 'B'], ['A', 'B'])
    t.bridge.handleStreamData(JSON.stringify({ changed: ['A', 'B'], fingerprints: {} }))
    expect(t.posted).toEqual([
      { type: 'ergo:changed', table: 'A' },
      { type: 'ergo:changed', table: 'B' },
    ])
    t.timers.advance(10_000)
    expect(t.reloads()).toBe(0)
  })

  test('other tables re-render once, 1 s after the last change', async () => {
    const t = setup()
    await t.ready(['A', 'B'], ['B'])
    t.bridge.handleChanged(['A'])
    t.timers.advance(RERENDER_DELAY_MS - 1)
    t.bridge.handleChanged(['A']) // restarts the debounce
    t.timers.advance(RERENDER_DELAY_MS - 1)
    expect(t.posted).toEqual([])
    t.timers.advance(1)
    expect(t.posted).toEqual([{ type: 'ergo:getscroll' }]) // asks for the scroll position first
    t.timers.advance(SCROLL_TIMEOUT_MS)
    expect(t.reloads()).toBe(1)
    t.timers.advance(10_000)
    expect(t.reloads()).toBe(1)
  })

  test('a baseline event with nothing changed does nothing', async () => {
    const t = setup()
    await t.ready(['A'])
    t.bridge.handleStreamData(JSON.stringify({ changed: [], fingerprints: { A: [1, 'x', 2] } }))
    t.bridge.handleStreamData('not json')
    t.bridge.handleStreamData(JSON.stringify({ nope: 1 }))
    t.timers.advance(10_000)
    expect(t.posted).toEqual([])
    expect(t.reloads()).toBe(0)
  })

  test('is deferred while a field has focus, and runs 1 s after it blurs', async () => {
    const t = setup()
    await t.ready(['A'])
    await t.send({ type: 'ergo:focus', focused: true })
    t.bridge.handleChanged(['A'])
    t.timers.advance(60_000)
    expect(t.posted).toEqual([])
    expect(t.reloads()).toBe(0)

    await t.send({ type: 'ergo:focus', focused: false })
    t.timers.advance(RERENDER_DELAY_MS - 1)
    expect(t.posted).toEqual([])
    t.timers.advance(1)
    expect(t.posted).toEqual([{ type: 'ergo:getscroll' }])
    t.timers.advance(SCROLL_TIMEOUT_MS)
    expect(t.reloads()).toBe(1)
  })

  test('blurring with nothing pending does not re-render', async () => {
    const t = setup()
    await t.send({ type: 'ergo:focus', focused: true })
    await t.send({ type: 'ergo:focus', focused: false })
    t.timers.advance(10_000)
    expect(t.reloads()).toBe(0)
    expect(t.posted).toEqual([])
  })

  test('ergo:reload from the page re-renders at once', async () => {
    const t = setup()
    await t.send({ type: 'ergo:reload' })
    expect(t.posted).toEqual([{ type: 'ergo:getscroll' }])
    await t.send({ type: 'ergo:scroll', y: 120 })
    expect(t.reloads()).toBe(1)
  })
})

describe('scroll', () => {
  test('restores the saved position after the reloaded page is ready', async () => {
    const t = setup()
    await t.ready(['A'])
    t.bridge.reloadNow()
    await t.send({ type: 'ergo:scroll', y: 480 })
    expect(t.reloads()).toBe(1)
    expect(t.posted).toEqual([{ type: 'ergo:getscroll' }]) // not restored before the new page is ready
    await t.ready(['A'])
    expect(t.posted.at(-1)).toEqual({ type: 'ergo:restore', y: 480 })
    await t.ready(['A', 'B']) // a later ready (more handlers) does not restore again
    expect(t.posted.filter(m => m.type === 'ergo:restore')).toHaveLength(1)
  })

  test('reloads without restoring when the page does not answer in time', async () => {
    const t = setup()
    t.bridge.reloadNow()
    t.timers.advance(SCROLL_TIMEOUT_MS - 1)
    expect(t.reloads()).toBe(0)
    t.timers.advance(1)
    expect(t.reloads()).toBe(1)
    await t.ready([])
    expect(t.posted.map(m => m.type)).toEqual(['ergo:getscroll'])
  })

  test('an unsolicited scroll message is ignored', async () => {
    const t = setup()
    await t.send({ type: 'ergo:scroll', y: 10 })
    expect(t.reloads()).toBe(0)
  })

  test('a reload asked for twice while waiting reloads once', async () => {
    const t = setup()
    t.bridge.reloadNow()
    t.bridge.reloadNow()
    await t.send({ type: 'ergo:scroll', y: 5 })
    t.timers.advance(10_000)
    expect(t.reloads()).toBe(1)
    expect(t.posted.filter(m => m.type === 'ergo:getscroll')).toHaveLength(1)
  })

  test('a reload from outside drops pending work and any saved position', async () => {
    const t = setup()
    await t.ready(['A'])
    t.bridge.handleChanged(['A'])
    t.bridge.frameReplaced()
    t.timers.advance(10_000)
    expect(t.posted).toEqual([])
    expect(t.reloads()).toBe(0)
  })
})

describe('stream tables', () => {
  test('reports the tables to stream when ready changes them', async () => {
    const t = setup()
    await t.ready(['B', 'A'])
    await t.ready(['A', 'B']) // same set after a reload: nothing to reopen
    await t.ready(['A', 'B'], ['C']) // the page registered ergo.on("table:C")
    await t.ready([])
    expect(t.streams).toEqual([['A', 'B'], ['A', 'B', 'C'], []])
  })

  test('builds the events url', () => {
    expect(eventsUrl('pantry', ['A', 'B'])).toBe('/api/bots/pantry/tables/events?tables=A,B')
    expect(eventsUrl('my bot', ['A b'])).toBe('/api/bots/my%20bot/tables/events?tables=A%20b')
  })

  test('parses stream data', () => {
    expect(parseStreamEvent('{"changed":["A"],"fingerprints":{}}')).toEqual(['A'])
    expect(parseStreamEvent('{"changed":[]}')).toEqual([])
    expect(parseStreamEvent('{"changed":"A"}')).toBeNull()
    expect(parseStreamEvent(null)).toBeNull()
  })
})
