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

class FakeEventSource {
  onmessage: ((event: { data: string }) => void) | null = null
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

test('scrolling the See more button into view loads older messages once', async () => {
  const calls: { id: string; before?: number }[] = []
  api.session = async (id, before) => {
    calls.push({ id, before })
    return before == null ? newest : older
  }
  await mount()
  expect(button()?.textContent).toBe('See more')
  expect(button()?.disabled).toBe(false)
  await act(async () => observers.at(-1)?.fire(true))
  expect(olderCalls(calls)).toEqual([])

  await scrollUp()
  scrolls.length = 0
  await act(async () => observers.at(-1)?.fire(true))
  expect(olderCalls(calls)).toEqual([{ id: 'thread', before: 50 }])
  expect(button()?.textContent).toBe('See more')
  expect(container.textContent).toContain('older page')
  expect(container.textContent).toContain('newest anchor')
  expect(transcript().scrollTop).toBe(40 + (400 - 100 - 80))
  expect(scrolls).toEqual([])
})

test('the button remains a manual fallback, including while the reader is at the bottom', async () => {
  const calls: { id: string; before?: number }[] = []
  api.session = async (id, before) => {
    calls.push({ id, before })
    return before == null ? newest : older
  }
  await mount()
  await act(async () => button()?.click())
  expect(olderCalls(calls)).toEqual([{ id: 'thread', before: 50 }])
  expect(button()?.getAttribute('type')).toBe('button')
})

test('a second trigger while loading does not start another request', async () => {
  const calls: { id: string; before?: number }[] = []
  const { promise: pending, resolve: release } = Promise.withResolvers<SessionDetail>()
  api.session = (id, before) => {
    calls.push({ id, before })
    return before == null ? Promise.resolve(newest) : pending
  }
  await mount()
  await scrollUp()
  await act(async () => observers.at(-1)?.fire(true))
  expect(button()?.disabled).toBe(true)
  expect(button()?.textContent).toBe('Loading…')
  expect(button()?.getAttribute('aria-busy')).toBe('true')
  await act(async () => {
    observers.at(-1)?.fire(true)
    button()?.click()
  })
  expect(olderCalls(calls)).toHaveLength(1)
  await act(async () => {
    release(older)
    await pending
  })
  expect(olderCalls(calls)).toHaveLength(1)
  expect(button()?.disabled).toBe(false)
})

test('an error does not retry until See more is pressed', async () => {
  const calls: { id: string; before?: number }[] = []
  let fail = true
  api.session = async (id, before) => {
    calls.push({ id, before })
    if (before != null && fail) throw new Error('History unavailable')
    return before == null ? newest : older
  }
  await mount()
  await scrollUp()
  await act(async () => observers.at(-1)?.fire(true))
  expect(container.textContent).toContain('History unavailable')
  expect(container.textContent).toContain('Press See more to try again.')
  expect(button()?.disabled).toBe(false)
  expect(button()?.title).toContain('try again')
  const failed = olderCalls(calls).length
  await act(async () => observers.at(-1)?.fire(true))
  expect(olderCalls(calls)).toHaveLength(failed)
  await act(async () => observers.at(-1)?.fire(false))
  await act(async () => observers.at(-1)?.fire(true))
  expect(olderCalls(calls)).toHaveLength(failed)
  fail = false
  await act(async () => button()?.click())
  expect(olderCalls(calls)).toHaveLength(failed + 1)
  expect(container.textContent).toContain('older page')
})
test('a still-visible button loads the next page, then stops when history is exhausted', async () => {
  buttonStaysVisible = true
  const calls: { id: string; before?: number }[] = []
  const pages = [older, oldest]
  api.session = async (id, before) => {
    calls.push({ id, before })
    return before == null ? newest : pages.shift()!
  }
  await mount()
  await scrollUp()
  await act(async () => observers.at(-1)?.fire(true))
  expect(olderCalls(calls).map(call => call.before)).toEqual([50, 40])
  expect(button()).toBeNull()
  expect(container.textContent).toContain('start of history')
  expect(container.textContent).toContain('older page')
  await act(async () => observers.at(-1)?.fire(true))
  expect(olderCalls(calls)).toHaveLength(2)
})

test('switching chats or unmounting drops an in-flight older page', async () => {
  const calls: { id: string; before?: number }[] = []
  const held = Promise.withResolvers<SessionDetail>()
  api.session = (id, before) => {
    calls.push({ id, before })
    if (before != null) return held.promise
    return Promise.resolve(id === 'thread' ? newest : detail(id, [message(3, 'other chat only')], false))
  }
  const router = await mount()
  await scrollUp()
  await act(async () => observers.at(-1)?.fire(true))
  expect(olderCalls(calls)).toEqual([{ id: 'thread', before: 50 }])
  await act(async () => {
    await router.navigate('/s/other')
  })
  await act(async () => {
    held.resolve(detail('thread', [message(1, 'stale older page')], false, 1))
    await held.promise
  })
  expect(container.textContent).toContain('other chat only')
  expect(container.textContent).not.toContain('stale older page')

  const again = Promise.withResolvers<SessionDetail>()
  api.session = (id, before) => {
    calls.push({ id, before })
    if (before != null) return again.promise
    return Promise.resolve(newest)
  }
  await act(async () => {
    await router.navigate('/s/thread')
  })
  await scrollUp()
  await act(async () => observers.at(-1)?.fire(true))
  act(() => root.unmount())
  expect(() => again.resolve(oldest)).not.toThrow()
})

test('phone and desktop both observe the transcript scroll root', async () => {
  api.session = async () => newest
  for (const phone of [false, true]) {
    window.matchMedia = (() => ({
      matches: phone,
      addEventListener() {},
      removeEventListener() {},
    })) as typeof window.matchMedia
    await mount(phone ? '/s/thread?layout=phone' : '/s/thread')
    const box = transcript()
    expect(box).not.toBeNull()
    expect(observers.at(-1)?.root === box || box.contains(observers.at(-1)?.target ?? null)).toBe(true)
    expect(observers.at(-1)?.target).toBe(button())
  }
})

function rect(top: number, bottom: number): DOMRect {
  return {
    top,
    bottom,
    left: 0,
    right: 200,
    width: 200,
    height: Math.max(0, bottom - top),
    x: 0,
    y: top,
    toJSON() {
      return {}
    },
  } as DOMRect
}
