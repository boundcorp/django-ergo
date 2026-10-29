import { afterEach, beforeEach, describe, expect, test } from 'bun:test'
import { act } from 'react'
import type { Root } from 'react-dom/client'
import { Window } from 'happy-dom'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { api, type Call, type Message, type SessionDetail } from '../api'
import { Chat } from './Chat'

let window: Window
let root: Root
let container: HTMLDivElement
let stream: FakeEventSource
const original = {
  session: api.session,
  send: api.send,
  attachments: api.attachments,
  models: api.models,
  pins: api.pins,
}

class FakeEventSource {
  onmessage: ((event: { data: string }) => void) | null = null
  constructor(_url: string) {
    stream = this
  }
  close() {}
  emit(messages: Message[], calls: Call[]) {
    act(() => this.onmessage?.({ data: JSON.stringify({ messages, calls }) }))
  }
}

function call(id: string, suggestions: string[] | undefined, overrides: Partial<Call> = {}): Call {
  return {
    id,
    kind: 'chat_reply',
    status: 'completed',
    request: '',
    response: { text: 'Reply', suggestions },
    error: '',
    first_sequence: 0,
    last_sequence: 1,
    model_name: '',
    input_tokens: 0,
    output_tokens: 0,
    turns_used: 1,
    pending_approvals: [],
    tools: [],
    created_at: `2026-01-01T00:00:0${id}Z`,
    ...overrides,
  }
}
function reply(line: number, suggestions?: string[]): Message {
  return {
    line,
    role: 'assistant',
    timestamp: null,
    blocks: [
      {
        type: 'tool_use',
        id: `tool-${line}`,
        name: 'send_reply',
        input: { text: 'Reply', ...(suggestions === undefined ? {} : { suggestions }) },
      },
    ],
  }
}
function detail(messages: Message[] = [], calls: Call[] = []): SessionDetail {
  return {
    debug_shortcut: false,
    session: {
      id: 'thread',
      bot: 'helper',
      title: 'Thread',
      role: 'thread',
      status: 'active',
      parent_id: null,
      username: 'demo',
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    },
    messages,
    calls,
  }
}
function pills(variant: 'desktop' | 'mobile') {
  return [...container.querySelectorAll(`.suggestion-${variant} .quick-reply-label`)].map(node => node.textContent)
}
async function mount(initial: SessionDetail) {
  api.session = async () => initial
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={['/s/thread']}>
        <Routes>
          <Route path="/s/:id" element={<Chat onChange={() => undefined} />} />
        </Routes>
      </MemoryRouter>,
    )
    await Promise.resolve()
  })
}

beforeEach(async () => {
  window = new Window({ url: 'http://localhost' })
  Object.assign(globalThis, {
    window,
    document: window.document,
    navigator: window.navigator,
    HTMLElement: window.HTMLElement,
    Event: window.Event,
    localStorage: window.localStorage,
    EventSource: FakeEventSource,
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
  window.HTMLElement.prototype.scrollTo = function () {}
  api.attachments = async () => []
  api.pins = async () => []
  api.models = async () => ({ models: [], default: '', default_engine_type: '' })
  api.send = async () => ({
    session_id: 'thread',
    call_id: null,
    type: null,
    text: '',
    suggestions: [],
    approvals: [],
    error: '',
    queued: true,
  })
  // React DOM captures the document during initialization, after Happy DOM is installed.
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

describe('Chat suggestions', () => {
  test('hides debug when the server does not permit it', async () => {
    await mount(detail())
    expect(container.querySelector('.chat-floating-actions')?.textContent).not.toContain('Debug')
  })

  test('shows debug beside Illustrate when the server permits it', async () => {
    await mount({ ...detail(), debug_shortcut: true })
    const actions = container.querySelector('.chat-floating-actions')!
    expect(actions.textContent).toContain('Debug')
    expect(actions.textContent).toContain('Illustrate')
    expect(actions.className).toContain('flex-wrap')
  })

  test('queued send receives live suggestions and replaces them on subsequent events', async () => {
    window.localStorage.setItem('ergonaut.draft.thread', 'Hello')
    let sends = 0
    const queuedSend = api.send
    api.send = async (...args) => {
      sends += 1
      return queuedSend(...args)
    }
    await mount(detail())
    await act(async () => {
      container
        .querySelector('form.chat-composer')!
        .dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }))
      await Promise.resolve()
    })
    expect(sends).toBe(1)
    stream.emit(
      [reply(1, ['First'])],
      [call('1', undefined, { status: 'in_progress', response: null, last_sequence: null })],
    )
    expect(pills('desktop')).toEqual([])
    expect(pills('mobile')).toEqual([])
    const first = call('1', ['First'])
    stream.emit([reply(1, ['First'])], [first])
    expect(pills('desktop')).toEqual(['First'])
    expect(pills('mobile')).toEqual(['First'])
    stream.emit([reply(1, ['First'])], [first])
    expect(pills('desktop')).toEqual(['First'])
    expect(pills('mobile')).toEqual(['First'])
    stream.emit([reply(2, ['Next', 'Next'])], [call('2', ['Next', 'Next'], { first_sequence: 2, last_sequence: 2 })])
    expect(pills('desktop')).toEqual(['Next', 'Next'])
    expect(pills('mobile')).toEqual(['Next', 'Next'])
    stream.emit([reply(2, ['Updated'])], [call('2', ['Updated'], { first_sequence: 2, last_sequence: 2 })])
    expect(pills('desktop')).toEqual(['Updated'])
    expect(pills('mobile')).toEqual(['Updated'])
  })
  test('history suggestions appear on both layouts', async () => {
    await mount(detail([reply(1, ['History'])], [call('1', undefined)]))
    expect(pills('desktop')).toEqual(['History'])
    expect(pills('mobile')).toEqual(['History'])
  })
  test('a later reply without suggestions and a user message clear previous pills', async () => {
    await mount(detail([reply(1, ['Old'])], [call('1', ['Old'])]))
    stream.emit([{ line: 2, role: 'user', timestamp: null, blocks: [{ type: 'text', text: 'Next' }] }], [])
    expect(pills('desktop')).toEqual([])
    expect(pills('mobile')).toEqual([])
    stream.emit(
      [],
      [call('2', undefined, { status: 'in_progress', response: null, first_sequence: 2, last_sequence: null })],
    )
    expect(pills('desktop')).toEqual([])
    expect(pills('mobile')).toEqual([])
    stream.emit([reply(3)], [call('2', undefined, { first_sequence: 2, last_sequence: 3 })])
    expect(pills('desktop')).toEqual([])
    expect(pills('mobile')).toEqual([])
  })
})
