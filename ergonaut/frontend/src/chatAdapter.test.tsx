import { afterEach, beforeEach, describe, expect, test } from 'bun:test'
import { act } from 'react'
import type { createRoot as CreateRoot, Root } from 'react-dom/client'
import { Window } from 'happy-dom'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import {
  api,
  type Approval,
  type Call,
  type Message,
  type SessionDetail,
  type Turn,
  type Worker,
  type AttachmentFile,
} from './api'
import {
  adapterMeta,
  ambiguousSend,
  approvalBatch,
  chooseApproval,
  fromAttachment,
  toAttachment,
  toChatMessages,
} from './chatAdapter'
import { ComposerVoiceProvider } from './components/VoiceRecorder'
import { Chat } from './pages/Chat'

// ---------------------------------------------------------------------------------------------------------------
// Pure adapter: Ergonaut records <-> @ergo/chat-ui model
// ---------------------------------------------------------------------------------------------------------------

const msg = (line: number, role: string, blocks: Message['blocks'], timestamp: string | null = null): Message => ({
  line,
  role,
  blocks,
  timestamp,
})

describe('toChatMessages', () => {
  test('ids are lines, roles map, timestamps become createdAt, other roles are system', () => {
    const [a, b, c] = toChatMessages([
      msg(4, 'user', [{ type: 'text', text: 'hi' }], '2026-01-01T00:00:00Z'),
      msg(5, 'assistant', [{ type: 'text', text: 'yo' }]),
      msg(6, 'tool', [{ type: 'text', text: 'x' }]),
    ])
    expect([a.id, a.role, a.createdAt]).toEqual(['4', 'user', '2026-01-01T00:00:00Z'])
    expect([b.id, b.role, b.createdAt]).toEqual(['5', 'assistant', undefined])
    expect(c.role).toBe('system')
    expect(a.parts).toEqual([{ type: 'text', text: 'hi' }])
  })

  test('a tool call and its later result become one tool part with the right status', () => {
    const out = toChatMessages([
      msg(1, 'assistant', [
        { type: 'tool_use', id: 'ok', name: 'a', input: { x: 1 } },
        { type: 'tool_use', id: 'bad', name: 'b', input: {} },
        { type: 'tool_use', id: 'open', name: 'c', input: {} },
      ]),
      msg(2, 'user', [
        { type: 'tool_result', tool_use_id: 'ok', content: 'fine' },
        { type: 'tool_result', tool_use_id: 'bad', content: 'boom', is_error: true },
      ]),
    ])
    expect(out[0].parts).toEqual([
      { type: 'tool', id: 'ok', name: 'a', status: 'done', input: { x: 1 }, result: 'fine' },
      { type: 'tool', id: 'bad', name: 'b', status: 'error', input: {}, result: 'boom' },
      { type: 'tool', id: 'open', name: 'c', status: 'running', input: {}, result: undefined },
    ])
    expect(out[1].parts).toEqual([]) // results are consumed by their calls
  })

  test('attachments, thinking and context map; meta.blocks stays aligned with parts', () => {
    const [m] = toChatMessages([
      msg(1, 'assistant', [
        { type: 'thinking', text: 'hmm' },
        { type: 'tool_result', tool_use_id: 'gone', content: '' },
        { type: 'attachment', label: 'a.png', id: 'f1', media_type: 'image/png' },
        { type: 'attachment', label: 'b.txt' },
        { type: 'context', text: 'ctx' },
      ]),
    ])
    expect(m.parts.map(p => p.type)).toEqual(['custom', 'attachment', 'attachment', 'custom'])
    expect(m.parts[1]).toEqual({
      type: 'attachment',
      attachment: { id: 'f1', name: 'a.png', mediaType: 'image/png' },
    })
    expect((m.parts[2] as { attachment: { id: string } }).attachment.id).toBe('1:3') // no id: stable fallback
    const { blocks, source } = adapterMeta(m)
    expect(source.line).toBe(1)
    expect(blocks.map(b => b.type)).toEqual(['thinking', 'attachment', 'attachment', 'context'])
    expect(blocks).toHaveLength(m.parts.length)
  })

  test('outgoing attachments round-trip through the package model', () => {
    for (const file of [
      { id: 'a', filename: 'a.png', image: true, audio: false },
      { id: 'b', filename: 'b.webm', image: false, audio: true },
      { id: 'c', filename: 'c.pdf', image: false, audio: false },
    ])
      expect(fromAttachment(toAttachment(file))).toEqual(file)
    expect(toAttachment({ id: 'a', filename: 'a', image: false, audio: false }).url).toBe(
      '/api/attachments/a/download?inline=true',
    )
  })
})

describe('approval batching', () => {
  const a: Approval = { id: 'a', name: 'run', input: { cmd: 'ls' } }
  const b: Approval = { id: 'b', name: 'write', input: { path: 'x' } }
  const empty = { batch: '', choices: {} }

  test('answers wait until every call has one, then go together with what was shown', () => {
    const first = chooseApproval(empty, 'call', [a, b], 'a', true)
    expect(first.submit).toBeNull()
    expect(first.state.choices).toEqual({ a: true })
    const second = chooseApproval(first.state, 'call', [a, b], 'b', false)
    expect(second.submit).toEqual({ a: true, b: false })
    expect(second.state.choices).toEqual({})
  })

  test('changing an answer before the last one replaces it', () => {
    const one = chooseApproval(empty, 'call', [a, b], 'a', true).state
    const changed = chooseApproval(one, 'call', [a, b], 'a', false)
    expect(changed.state.choices).toEqual({ a: false })
    expect(chooseApproval(changed.state, 'call', [a, b], 'b', true).submit).toEqual({ a: false, b: true })
  })

  test('a stale batch (new call, or the same call asking for something else) drops earlier answers', () => {
    const one = chooseApproval(empty, 'call', [a, b], 'a', true).state
    const newCall = chooseApproval(one, 'call-2', [a, b], 'b', true)
    expect(newCall.submit).toBeNull()
    expect(newCall.state.choices).toEqual({ b: true })
    const edited = chooseApproval(one, 'call', [{ ...a, input: { cmd: 'rm -rf /' } }, b], 'b', true)
    expect(edited.submit).toBeNull()
    expect(edited.state.choices).toEqual({ b: true })
  })

  test('a single waiting call submits at once; batch identity covers call and contents', () => {
    expect(chooseApproval(empty, 'call', [a], 'a', true).submit).toEqual({ a: true })
    expect(approvalBatch('call', [a])).not.toBe(approvalBatch('call', [b]))
    expect(approvalBatch('c1', [a])).not.toBe(approvalBatch('c2', [a]))
  })
})

test('ambiguousSend says the send may have succeeded', () => {
  expect(ambiguousSend('Failed to fetch')).toBe(
    'Failed to fetch Send may have succeeded; check the conversation before retrying.',
  )
})

// ---------------------------------------------------------------------------------------------------------------
// The chat screen: real Chat, Transcript and Composer, with the API mocked
// ---------------------------------------------------------------------------------------------------------------

const BOX = '.chat-composer textarea'
const original = { ...api }
let window: Window
let root: Root
let container: HTMLDivElement
let createRoot: typeof CreateRoot
let detail: SessionDetail
let sends: { text: string; ids: string[]; mode: string }[]
let calls: Record<string, unknown[][]>

function session(extra: Partial<SessionDetail['session']> = {}): SessionDetail['session'] {
  return {
    id: 'chat-1',
    bot: 'helper',
    title: 'Chat one',
    role: 'thread',
    status: 'active',
    username: 'owner',
    parent_id: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...extra,
  }
}
function call(extra: Partial<Call> = {}): Call {
  return {
    id: 'call-1',
    kind: 'chat_reply',
    status: 'completed',
    request: '',
    response: null,
    error: '',
    first_sequence: 0,
    last_sequence: 99,
    model_name: 'm',
    input_tokens: 1,
    output_tokens: 1,
    turns_used: 1,
    pending_approvals: [],
    tools: [],
    created_at: '2026-01-01T00:00:00Z',
    ...extra,
  } as Call
}
function turn(extra: Partial<Turn> = {}): Turn {
  return {
    session_id: 'chat-1',
    call_id: null,
    type: null,
    text: '',
    suggestions: [],
    approvals: [],
    error: '',
    ...extra,
  }
}
const text = (t: string) => ({ type: 'text' as const, text: t })

function record<T extends keyof typeof api>(name: T, impl: (...args: never[]) => unknown) {
  ;(api as Record<string, unknown>)[name] = (...args: unknown[]) => {
    ;(calls[name] ??= []).push(args)
    return impl(...(args as never[]))
  }
}

function q<T extends Element = HTMLElement>(selector: string) {
  const found = container.querySelector<T>(selector)
  if (!found) throw new Error(`Missing ${selector}`)
  return found
}
function button(label: string) {
  const found =
    [...container.querySelectorAll('button')].find(
      item => item.textContent?.trim() === label || item.getAttribute('aria-label') === label,
    ) ?? [...container.querySelectorAll('button')].find(item => item.textContent?.startsWith(label))
  if (!found) throw new Error(`Missing ${label} button`)
  return found as HTMLButtonElement
}
async function click(el: Element) {
  await act(async () => {
    ;(el as HTMLElement).click()
  })
}
async function settle() {
  await act(async () => {
    await new Promise(resolve => setTimeout(resolve, 0))
  })
}
async function typeText(value: string) {
  const area = q<HTMLTextAreaElement>(BOX)
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')!.set!
    setter.call(area, value)
    area.dispatchEvent(new window.Event('input', { bubbles: true }) as unknown as Event)
  })
}
async function enter(init: Record<string, unknown> = {}) {
  await act(async () => {
    q(BOX).dispatchEvent(
      new window.KeyboardEvent('keydown', {
        key: 'Enter',
        bubbles: true,
        cancelable: true,
        ...init,
      }) as unknown as Event,
    )
  })
  await settle()
}
async function pick(files: File[]) {
  const input = q<HTMLInputElement>('input[type=file]')
  await act(async () => {
    Object.defineProperty(input, 'files', { value: files, configurable: true })
    input.dispatchEvent(new window.Event('change', { bubbles: true }) as unknown as Event)
  })
  await settle()
}
async function mount() {
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={['/s/chat-1']}>
        <ComposerVoiceProvider>
          <Routes>
            <Route path="/s/:id" element={<Chat onChange={() => {}} />} />
          </Routes>
        </ComposerVoiceProvider>
      </MemoryRouter>,
    )
  })
  await settle()
}

function file(id: string, filename: string, extra: Partial<AttachmentFile> = {}): AttachmentFile {
  return { id, filename, kind: 'file', media_type: 'application/pdf', ...extra } as AttachmentFile
}

beforeEach(async () => {
  window = new Window({ url: 'http://localhost' })
  Object.assign(globalThis, {
    window,
    document: window.document,
    navigator: window.navigator,
    sessionStorage: window.sessionStorage,
    localStorage: window.localStorage,
    HTMLElement: window.HTMLElement,
    HTMLDialogElement: window.HTMLDialogElement,
    Event: window.Event,
    MouseEvent: window.MouseEvent,
    Node: window.Node,
    EventSource: class {
      close() {}
    },
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
  createRoot = (await import('react-dom/client')).createRoot
  container = window.document.createElement('div')
  window.document.body.append(container)
  root = createRoot(container)
  detail = { debug_shortcut: false, session: session(), messages: [], calls: [] }
  sends = []
  calls = {}
  record('realtimeVoiceConfig', async () => ({ enabled: false, standby_enabled: false }))
  record('realtimeVoiceSessions', async () => [])
  record('session', async () => detail)
  record('models', async () => ({ models: [], default: '', default_engine_type: '' }))
  record('attachments', async () => [])
  record('pins', async () => [])
  record('send', async (_id: string, t: string, ids: string[] = [], mode = 'send') => {
    sends.push({ text: t, ids, mode })
    return turn()
  })
  record('stop', async () => turn())
  record('deleteAttachment', async () => ({ ok: true }))
})

afterEach(() => {
  act(() => root.unmount())
  Object.assign(api, original)
  window.happyDOM.close()
})

describe('transcript: Ergonaut cards render through the package slots', () => {
  test('text, reply bubble, hidden echo and message anchors', async () => {
    detail.messages = [
      msg(0, 'user', [text('hello bot')], '2026-01-01T10:00:00Z'),
      msg(1, 'assistant', [
        text('echo text'),
        {
          type: 'tool_use',
          id: 'r',
          name: 'send_reply',
          input: { type: 'question', text: 'echo text', suggestions: ['yes', 'no'] },
        },
      ]),
    ]
    detail.calls = [call({ first_sequence: 0, last_sequence: 1, response: { suggestions: ['yes', 'no'] } })]
    await mount()
    const html = container.innerHTML
    expect(q('#m-0').textContent).toContain('hello bot')
    expect(q('#m-1').textContent).toContain('Question')
    expect(container.textContent!.match(/echo text/g)).toHaveLength(1) // the stored echo is hidden, the reply bubble shows it
    expect(html).toContain('Suggested: yes · no')
    expect(container.querySelector('[role=log]')).toBeNull() // Ergonaut owns the scroller; the package list is plain
  })

  test('thread messaging calls, worker cards and tool calls keep their cards', async () => {
    const worker = { id: 'w1', title: 'Build the thing', status: 'running', progress: '' } as unknown as Worker
    detail.workers = [worker]
    detail.messages = [
      msg(0, 'user', [text('go')]),
      msg(1, 'assistant', [
        {
          type: 'tool_use',
          id: 't1',
          name: 'ergo_thread_send',
          input: { thread: 'new', message: 'please help', bot: 'coder' },
        },
        { type: 'tool_use', id: 'w', name: 'ergo_agent_start', input: {} },
        { type: 'tool_use', id: 'plain', name: 'files_read', input: { path: '/a' } },
      ]),
      msg(2, 'user', [
        {
          type: 'tool_result',
          tool_use_id: 't1',
          content: JSON.stringify({
            thread_id: '12345678-1234-1234-1234-123456789abc',
            title: 'Helper thread',
            status: 'queued',
          }),
        },
        { type: 'tool_result', tool_use_id: 'w', content: JSON.stringify({ id: 'w1' }) },
        { type: 'tool_result', tool_use_id: 'plain', content: 'contents' },
      ]),
    ]
    detail.calls = [call()]
    await mount()
    const t = q('#m-1').textContent!
    expect(t).toContain('Started thread')
    expect(t).toContain('Helper thread')
    expect(t).toContain('please help')
    expect(t).toContain('Build the thing') // worker card
    expect(t).toContain('files_read') // ordinary tool card
    expect(q('#m-2').textContent).toBe('') // results are carried by their calls
  })

  test('long tool runs fold; errors and calls waiting on approval stay visible; the fold opens', async () => {
    const uses = Array.from({ length: 8 }, (_, i) => ({
      type: 'tool_use' as const,
      id: `c${i}`,
      name: `tool_${i}`,
      input: {},
    }))
    detail.messages = [
      msg(0, 'user', [text('work')]),
      msg(1, 'assistant', uses),
      msg(2, 'user', [{ type: 'tool_result', tool_use_id: 'c1', content: 'bad', is_error: true }]),
    ]
    detail.calls = [
      call({
        status: 'awaiting_approval',
        first_sequence: 0,
        last_sequence: null,
        pending_approvals: [{ id: 'c2', name: 'tool_2', input: {} }],
      }),
    ]
    await mount()
    const t = q('#m-1').textContent!
    // 8 calls, last 3 visible, the failed one (c1) and the one waiting on approval (c2) pinned: 8 - 3 - 2 = 3 folded
    expect(t).toContain('3 more calls')
    for (const name of ['tool_1', 'tool_2', 'tool_5', 'tool_6', 'tool_7']) expect(t).toContain(name)
    expect(t).toContain('tool_0, tool_3, tool_4') // the fold's summary, not three cards
    await click(button('+3 more calls'))
    const open = q('#m-1').textContent!
    expect(open).toContain('Hide 3 earlier calls')
    expect(open).not.toContain('tool_0, tool_3, tool_4')
    for (const name of ['tool_0', 'tool_3', 'tool_4']) expect(open).toContain(name)
  })

  test('event, attributed and resumed messages and compaction dividers keep their cards', async () => {
    detail.messages = [
      { ...msg(0, 'user', [text('Build finished')]), author: { kind: 'system', ref: 'ci', display_name: 'CI' } },
      {
        ...msg(1, 'user', [text('forwarded body')]),
        author: { kind: 'bot', ref: 'b', display_name: 'Other bot' },
        provenance: {
          kind: 'forwarded',
          origin: { session_id: 'abcdefab-1234-1234-1234-123456789abc', label: 'Origin chat', timestamp: null },
        },
      } as Message,
      msg(2, 'user', [text('[Resume] Your last turn stopped before it finished (a limit).')]),
    ]
    detail.compactions = [
      { id: 'k1', upto_sequence: 2, message_count: 40, reason: 'size', created_at: '2026-01-01T00:00:00Z' },
    ]
    detail.calls = [call()]
    await mount()
    expect(q('#m-0').textContent).toContain('CI · Event')
    expect(q('#m-0').textContent).toContain('Automated notification')
    expect(q('#m-1').textContent).toContain('Forwarded from')
    expect(q('#m-1').textContent).toContain('Origin chat')
    expect(q('#m-2').textContent).toContain('↻ Resumed after: a limit')
    expect(q('#m-2').textContent).toContain('40') // the compaction divider after line 2
  })

  test('a bot message with attachments shows its gallery once, at the first file', async () => {
    detail.messages = [
      msg(0, 'assistant', [
        text('files:'),
        { type: 'attachment', label: 'one.png', id: 'f1', media_type: 'image/png' },
        { type: 'attachment', label: 'two.pdf', id: 'f2', media_type: 'application/pdf' },
      ]),
    ]
    await mount()
    const t = q('#m-0').textContent!
    expect(q('#m-0').innerHTML).toContain('one.png')
    expect(t).toContain('two.pdf')
    expect(container.querySelectorAll('#m-0 img').length).toBeLessThanOrEqual(1)
  })
})

describe('composer: send, stop, interrupt, errors', () => {
  test('Enter sends the trimmed draft once with no attachments, then clears the box', async () => {
    await mount()
    expect(button('Send').disabled).toBe(true)
    await typeText('  hello there  ')
    expect(button('Send').disabled).toBe(false)
    await enter()
    expect(sends).toEqual([
      { text: '  hello there  '.trim() === 'hello there' ? sends[0]?.text : '', ids: [], mode: 'send' },
    ])
    expect(sends[0].text.trim()).toBe('hello there')
    expect(q<HTMLTextAreaElement>(BOX).value).toBe('')
  })

  test('Shift+Enter and an IME confirmation do not send', async () => {
    await mount()
    await typeText('draft')
    await enter({ shiftKey: true })
    await enter({ isComposing: true })
    expect(sends).toHaveLength(0)
    expect(q<HTMLTextAreaElement>(BOX).value).toBe('draft')
  })

  test('typing while the request is in flight survives: only the text that was sent is cleared', async () => {
    let release: () => void = () => {}
    record('send', async (_id: string, t: string) => {
      sends.push({ text: t, ids: [], mode: 'send' })
      await new Promise<void>(resolve => (release = resolve))
      return turn()
    })
    await mount()
    await typeText('first')
    await enter()
    await typeText('first and more')
    await act(async () => release())
    await settle()
    expect(sends.map(s => s.text)).toEqual(['first'])
    expect(q<HTMLTextAreaElement>(BOX).value).toBe('first and more')
  })

  test('a running turn shows Stop and "Stop & send"; Stop asks the server to stop; the placeholder says it steers', async () => {
    detail.messages = [msg(0, 'user', [text('go')])]
    detail.calls = [call({ status: 'in_progress', last_sequence: null })]
    await mount()
    expect(q<HTMLTextAreaElement>(BOX).placeholder).toBe('Steer the bot: your message joins this turn')
    expect([...container.querySelectorAll('button')].some(b => b.textContent === 'Stop & send')).toBe(false) // nothing to send yet
    await typeText('use the other file')
    expect(button('Stop & send').disabled).toBe(false)
    await click(button('Stop'))
    expect(calls.stop).toEqual([['chat-1']])
    expect(button('Stop').disabled).toBe(true) // stop requested, the turn ends at its next step
  })

  test('while running, Send steers the turn ("send") and "Stop & send" interrupts', async () => {
    detail.messages = [msg(0, 'user', [text('go')])]
    detail.calls = [call({ status: 'in_progress', last_sequence: null })]
    await mount()
    await typeText('steer')
    await enter()
    await typeText('actually this')
    await click(button('Stop & send'))
    await settle()
    expect(sends.map(s => [s.text, s.mode])).toEqual([
      ['steer', 'send'],
      ['actually this', 'interrupt'],
    ])
  })

  test('a failed request shows the error and keeps the draft', async () => {
    record('send', async () => {
      throw new Error('Server said no')
    })
    await mount()
    await typeText('keep me')
    await enter()
    expect(container.textContent).toContain('Server said no')
    expect(q<HTMLTextAreaElement>(BOX).value).toBe('keep me')
  })

  test('ambiguous send: a transport failure after the draft was accepted warns it may have gone through', async () => {
    let first = true
    record('send', async (_id: string, t: string) => {
      sends.push({ text: t, ids: [], mode: 'send' })
      if (first) {
        first = false
        throw new Error('Failed to fetch')
      }
      return turn()
    })
    await mount()
    await typeText('maybe sent')
    await enter()
    const shown = container.textContent!
    expect(shown).toContain('Failed to fetch')
    expect(shown).toContain('Send may have succeeded; check the conversation before retrying.')
  })

  test('a closed session or a pending approval disables the composer with a reason', async () => {
    detail.session = session({ status: 'completed', role: 'main' })
    await mount()
    expect(q<HTMLTextAreaElement>(BOX).disabled).toBe(true)
    expect(q<HTMLTextAreaElement>(BOX).placeholder).toBe('This session is closed')
  })
})

describe('composer: attachments', () => {
  test('picking uploads to the session, shows a chip, and the id goes with the next message', async () => {
    record('uploadAttachment', async (_id: string, f: File) =>
      file('up1', f.name, { kind: 'image', media_type: 'image/png' }),
    )
    await mount()
    await pick([new window.File(['x'], 'shot.png') as unknown as File])
    expect(calls.uploadAttachment?.length).toBe(1)
    expect(q('.chat-composer').textContent).toContain('shot.png')
    expect(button('Send').disabled).toBe(false) // an attachment alone is sendable
    await enter()
    expect(sends).toEqual([{ text: '', ids: ['up1'], mode: 'send' }])
    expect(q('.chat-composer').textContent).not.toContain('shot.png')
  })

  test('removing a chip drops it at once and deletes the stored file', async () => {
    record('uploadAttachment', async (_id: string, f: File) => file('up1', f.name))
    await mount()
    await pick([new window.File(['x'], 'doc.pdf') as unknown as File])
    await click(q('.chat-composer button[title=Remove]'))
    await settle()
    expect(q('.chat-composer').textContent).not.toContain('doc.pdf')
    expect(calls.deleteAttachment).toEqual([['up1']])
  })

  test('a failure part-way keeps the files already stored and reports the error', async () => {
    let n = 0
    record('uploadAttachment', async (_id: string, f: File) => {
      if (++n === 2) throw new Error('File too large')
      return file(`up${n}`, f.name)
    })
    await mount()
    await pick([new window.File(['x'], 'a.pdf') as unknown as File, new window.File(['y'], 'b.pdf') as unknown as File])
    expect(q('.chat-composer').textContent).toContain('a.pdf')
    expect(q('.chat-composer').textContent).not.toContain('b.pdf')
    expect(container.textContent).toContain('File too large')
  })
})

describe('approvals', () => {
  const waiting: Approval[] = [
    { id: 'a', name: 'run_shell', input: { cmd: 'ls' } },
    { id: 'b', name: 'write_file', input: { path: 'x' }, preview: 'diff preview' },
  ]
  function pausedCall() {
    detail.messages = [
      msg(0, 'user', [text('do it')]),
      msg(
        1,
        'assistant',
        waiting.map(w => ({ type: 'tool_use' as const, id: w.id, name: w.name, input: w.input })),
      ),
    ]
    detail.calls = [
      call({ status: 'awaiting_approval', first_sequence: 0, last_sequence: null, pending_approvals: waiting }),
    ]
  }

  test('the panel lists every waiting call, shows previews, and the transcript marks the calls as needing approval', async () => {
    pausedCall()
    await mount()
    expect(container.textContent).toContain('Tool approval requested: run_shell')
    expect(container.textContent).toContain('Tool approval requested: write_file')
    expect(container.textContent).toContain('diff preview')
    expect(q('#m-1').textContent).toContain('run_shell')
    expect(q<HTMLTextAreaElement>(BOX).placeholder).toBe('Answer the approval first')
    expect(q<HTMLTextAreaElement>(BOX).disabled).toBe(true)
  })

  test('answers are held until every call has one, then sent together with what was shown', async () => {
    pausedCall()
    record('decide', async () => turn())
    await mount()
    await click(button('Approve'))
    expect(calls.decide).toBeUndefined()
    expect(container.textContent).toContain('Approved')
    expect(container.textContent).toContain('Sent once every call has an answer')
    const rejects = [...container.querySelectorAll('button')].filter(b => b.textContent === 'Reject')
    await click(rejects[1])
    await settle()
    expect(calls.decide).toEqual([['chat-1', { a: true, b: false }, waiting, 'call-1']])
  })

  test('re-check asks the server to revalidate the call with the approvals that were shown', async () => {
    pausedCall()
    record('revalidateApproval', async () => turn())
    await mount()
    await click(button('Re-check permissions and run if authorized'))
    await settle()
    expect(calls.revalidateApproval).toEqual([['chat-1', 'call-1', waiting]])
  })
})
