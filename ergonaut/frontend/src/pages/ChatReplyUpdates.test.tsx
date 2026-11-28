import { afterEach, beforeEach, expect, test } from 'bun:test'
import { act } from 'react'
import type { Root } from 'react-dom/client'
import { Window } from 'happy-dom'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { api, type Message, type SessionDetail } from '../api'
import { ComposerVoiceProvider } from '../components/VoiceRecorder'
import { Chat } from './Chat'

let window: Window
let root: Root
let container: HTMLDivElement
let observers: FakeObserver[]
let scrolls: number[]
let buttonStaysVisible = false
const original = {
  session: api.session,
  attachments: api.attachments,
  models: api.models,
  pins: api.pins,
  setReplyUpdates: api.setReplyUpdates,
}

class FakeObserver {
  root: Element | null
  target: Element | null = null
  callback: IntersectionObserverCallback
  constructor(callback: IntersectionObserverCallback, options?: IntersectionObserverInit) {
    this.callback = callback
    this.root = (options?.root as Element | null) ?? null
    observers.push(this)
  }
  observe(target: Element) {
    this.target = target
  }
  unobserve() {
    this.target = null
  }
  disconnect() {
    this.target = null
  }
  takeRecords(): IntersectionObserverEntry[] {
    return []
  }
  fire(isIntersecting: boolean) {
    if (!this.target) return
    this.callback(
      [
        {
          isIntersecting,
          target: this.target,
          intersectionRatio: isIntersecting ? 1 : 0,
        } as IntersectionObserverEntry,
      ],
      this as unknown as IntersectionObserver,
    )
  }
}

let streams: FakeEventSource[] = []
class FakeEventSource {
  onmessage: ((event: { data: string }) => void) | null = null
  constructor() {
    streams.push(this)
  }
  close() {}
}

function message(line: number, text: string): Message {
  return {
    line,
    role: 'assistant',
    timestamp: '2026-01-01T00:00:00Z',
    blocks: [{ type: 'text', text }],
  }
}

function detail(id: string, messages: Message[], hasMore: boolean, first = messages[0]?.line ?? 0): SessionDetail {
  return {
    debug_shortcut: false,
    session: {
      id,
      bot: 'helper',
      title: id,
      role: 'thread',
      status: 'active',
      parent_id: null,
      username: 'demo',
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    },
    messages,
    calls: [],
    has_more: hasMore,
    first_line: first,
    message_count: hasMore ? first + messages.length + 40 : messages.length,
  }
}

const newest = detail('thread', [message(50, 'newest anchor'), message(51, 'middle'), message(52, 'latest')], true)
const older = detail('thread', [message(40, 'older page'), message(49, 'just before')], true, 40)
const oldest = detail('thread', [message(0, 'start of history')], false, 0)

function button() {
  return container.querySelector('[data-see-more]') as HTMLButtonElement | null
}
function transcript() {
  return container.querySelector('.chat-transcript') as HTMLDivElement
}
function olderCalls(calls: { id: string; before?: number }[]) {
  return calls.filter(call => call.before != null)
}

async function mount(path = '/s/thread') {
  const router = createMemoryRouter(
    [
      {
        path: '/s/:id',
        element: (
          <ComposerVoiceProvider>
            <Chat onChange={() => undefined} />
          </ComposerVoiceProvider>
        ),
      },
    ],
    { initialEntries: [path] },
  )
  await act(async () => {
    root.render(<RouterProvider router={router} />)
    await Promise.resolve()
  })
  return router
}

async function scrollUp() {
  const box = transcript()
  Object.defineProperty(box, 'scrollHeight', { configurable: true, value: 2000 })
  Object.defineProperty(box, 'clientHeight', { configurable: true, value: 400 })
  box.scrollTop = 40
  await act(async () => {
    box.dispatchEvent(new window.Event('scroll'))
  })
}

beforeEach(async () => {
  window = new Window({ url: 'http://localhost' })
  observers = []
  scrolls = []
  streams = []
  buttonStaysVisible = false
  Object.assign(globalThis, {
    window,
    document: window.document,
    navigator: window.navigator,
    HTMLElement: window.HTMLElement,
    Event: window.Event,
    localStorage: window.localStorage,
    EventSource: FakeEventSource,
    IntersectionObserver: FakeObserver,
    ResizeObserver: class {
      observe() {}
      disconnect() {}
    },
    IS_REACT_ACT_ENVIRONMENT: true,
  })
  window.matchMedia = (() => ({
    matches: false,
    addEventListener() {},
    removeEventListener() {},
  })) as typeof window.matchMedia
  window.HTMLElement.prototype.scrollTo = function (options?: ScrollToOptions) {
    if (options && typeof options.top === 'number') scrolls.push(options.top)
  }
  window.HTMLElement.prototype.getBoundingClientRect = function () {
    if (this.id === 'm-50') {
      const box = this.closest('.chat-transcript')
      const shifted = (box?.querySelectorAll('[id^="m-"]').length ?? 0) > 3
      const top = shifted ? 400 : 180
      return rect(top, top + 40)
    }
    if (this.classList?.contains('chat-transcript')) return rect(100, 500)
    if (this.hasAttribute('data-see-more')) {
      const box = this.closest('.chat-transcript')
      const shifted = (box?.querySelectorAll('[id^="m-"]').length ?? 0) > 3
      if (shifted && !buttonStaysVisible) return rect(20, 44)
      return rect(110, 140)
    }
    return rect(0, 0)
  }
  api.pins = async () => []
  api.models = async () => ({ models: [], default: '', default_engine_type: '' })
  // React DOM binds `document` when the module is first evaluated, so this import has to follow the Window.
  const { createRoot } = await import('react-dom/client')
  container = window.document.createElement('div')
  window.document.body.append(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => root.unmount())
  Object.assign(api, original)
  window.happyDOM.close()
})

// Reply updates (conversation.reply_updates) in the live chat: the per-chat switch, and an
// accepted revision arriving over the event stream while the reader follows or has scrolled up.

const tool = (line: number, id: string, name: string, input: object): Message => ({
  line,
  role: 'assistant',
  timestamp: '2026-01-01T00:00:00Z',
  blocks: [{ type: 'tool_use', id, name, input }],
})
const ok = (line: number, id: string, content: string): Message => ({
  line,
  role: 'user',
  timestamp: '2026-01-01T00:00:00Z',
  blocks: [{ type: 'tool_result', tool_use_id: id, content }],
})
const start = [
  tool(1, 'i1', 'ergo_interim_reply', { action: 'append', text: 'Starting' }),
  ok(2, 'i1', '{"reply_id":"i1","revision":0,"action":"append"}'),
]
const revised = [
  tool(3, 'u1', 'ergo_interim_reply', { action: 'update', reply_id: 'i1', revision: 0, text: 'Half done' }),
  ok(4, 'u1', '{"reply_id":"i1","revision":1,"action":"update"}'),
]
function live(messages: Message[]) {
  act(() => streams[streams.length - 1].onmessage?.({ data: JSON.stringify({ messages, calls: [] }) }))
}
function bubble() {
  return container.querySelector('[data-reply-id="i1"]')!
}

test('an update arriving while scrolled up keeps the reader in place and the reply node', async () => {
  api.session = async () => detail('thread', start, false)
  api.attachments = async () => []
  await mount()
  const node = bubble()
  await scrollUp()
  const before = scrolls.length
  live(revised)
  expect(bubble()).toBe(node)
  expect(bubble().textContent).toContain('Half done')
  expect(container.querySelectorAll('[data-reply-id]')).toHaveLength(1)
  expect(scrolls.length).toBe(before)
})

test('an update arriving while following keeps following', async () => {
  api.session = async () => detail('thread', start, false)
  api.attachments = async () => []
  await mount()
  const before = scrolls.length
  live(revised)
  expect(bubble().textContent).toContain('Half done')
  expect(scrolls.length).toBeGreaterThan(before)
})

test('the thread options switch reply updates for this chat', async () => {
  const sent: { id: string; enabled: boolean }[] = []
  const base = detail('thread', start, false)
  api.session = async () => base
  api.attachments = async () => []
  api.setReplyUpdates = async (id, enabled) => {
    sent.push({ id, enabled })
    return { ...base.session, reply_updates: enabled }
  }
  await mount()
  const toggle = () => container.querySelector('section[aria-label="Reply updates"] button') as HTMLButtonElement
  if (!toggle()) {
    await act(async () => {
      ;(container.querySelector('[data-options-button]') as HTMLButtonElement).click()
    })
  }
  expect(toggle().getAttribute('aria-pressed')).toBe('false')
  expect(toggle().textContent).toContain('Turn on reply updates')
  await act(async () => {
    toggle().click()
    await Promise.resolve()
  })
  expect(sent).toEqual([{ id: 'thread', enabled: true }])
  if (!toggle()) {
    await act(async () => {
      ;(container.querySelector('[data-options-button]') as HTMLButtonElement).click()
    })
  }
  expect(toggle().getAttribute('aria-pressed')).toBe('true')
})
