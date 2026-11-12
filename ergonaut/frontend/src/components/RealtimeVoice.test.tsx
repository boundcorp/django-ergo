import { afterEach, beforeEach, expect, spyOn, test } from 'bun:test'
import { act, createElement } from 'react'
import type { createRoot as CreateRoot, Root } from 'react-dom/client'
import { renderToStaticMarkup } from 'react-dom/server'
import { Window } from 'happy-dom'
import { LocalParticipant, Room, RoomEvent, Track } from 'livekit-client'
import { Link, MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom'
import { api, type RealtimeVoiceSession, type SessionDetail } from '../api'
import { ActiveVoiceCall, VoiceCallProvider, VoicePage, VoiceTranscript } from './RealtimeVoice'
import { VoiceSessionCard } from './VoiceSessionCard'
import { Chat } from '../pages/Chat'

const original = {
  config: api.realtimeVoiceConfig,
  sessions: api.realtimeVoiceSessions,
  start: api.startRealtimeVoice,
  session: api.realtimeVoiceSession,
  token: api.realtimeVoiceToken,
  end: api.endRealtimeVoice,
  standby: api.standbyRealtimeVoice,
  resume: api.resumeRealtimeVoice,
  detail: api.session,
  models: api.models,
  attachments: api.attachments,
  pins: api.pins,
}
const session: RealtimeVoiceSession = {
  id: 'voice-1',
  chat_id: 'chat-1',
  status: 'active',
  events: [{ event_id: 'event-1', kind: 'user', text: 'Hello', delivery: 'unknown', created_at: '' }],
}
function thread(id = 'chat-1'): SessionDetail {
  return {
    session: {
      id,
      bot: 'helper',
      title: `Exact title ${id}`,
      role: 'thread',
      status: 'active',
      username: 'owner',
      parent_id: null,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    },
    messages: [],
    calls: [],
  }
}
let window: Window
let root: Root
let container: HTMLDivElement
let createRoot: typeof CreateRoot
let restoreOutputs = () => {}
const restoreRoomMocks: (() => void)[] = []

function button(label: string) {
  const found = [...container.querySelectorAll('button')].find(item => item.textContent?.trim() === label)
  if (!found) throw new Error(`Missing ${label} button`)
  return found
}
function link(label: string) {
  const found = [...container.querySelectorAll('a')].find(item => item.textContent?.trim() === label)
  if (!found) throw new Error(`Missing ${label} link`)
  return found
}
function Navigation() {
  const navigate = useNavigate()
  return (
    <nav>
      <Link to="/s/chat-1/voice">Voice fixture</Link>
      <Link to="/s/chat-2/voice">Other thread voice</Link>
      <button onClick={() => navigate(-1)}>Browser back</button>
    </nav>
  )
}
function VoiceHarness({ initial = '/s/chat-1/voice', realChat = false }: { initial?: string; realChat?: boolean }) {
  return (
    <MemoryRouter initialEntries={[initial]}>
      <VoiceCallProvider>
        <Navigation />
        <ActiveVoiceCall />
        <Routes>
          <Route path="/s/:id/voice" element={<VoicePage />} />
          <Route path="/s/:id" element={realChat ? <Chat onChange={() => {}} /> : <p>Thread view</p>} />
        </Routes>
      </VoiceCallProvider>
    </MemoryRouter>
  )
}
async function mountVoice() {
  await act(async () => {
    root.render(<VoiceHarness />)
  })
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
  window.HTMLDialogElement.prototype.showModal = function () {
    this.open = true
  }
  window.HTMLDialogElement.prototype.close = function () {
    this.open = false
  }
  // React DOM captures document at module initialization, after this test installs Happy DOM.
  createRoot = (await import('react-dom/client')).createRoot
  container = window.document.createElement('div')
  window.document.body.append(container)
  root = createRoot(container)
  api.realtimeVoiceConfig = async () => ({ enabled: true, standby_enabled: true })
  api.realtimeVoiceSessions = async id => (id === 'chat-1' ? [session] : [])
  api.realtimeVoiceSession = async () => session
  api.startRealtimeVoice = async () => session
  api.realtimeVoiceToken = async () => {
    throw new Error('Token unavailable')
  }
  api.session = async id => thread(id)
  api.models = async () => ({ models: [], default: '', default_engine_type: '' })
  api.attachments = async () => []
  api.pins = async () => []
})

afterEach(() => {
  act(() => root.unmount())
  api.realtimeVoiceConfig = original.config
  api.startRealtimeVoice = original.start
  api.realtimeVoiceSessions = original.sessions
  api.realtimeVoiceSession = original.session
  api.realtimeVoiceToken = original.token
  api.endRealtimeVoice = original.end
  api.standbyRealtimeVoice = original.standby
  api.resumeRealtimeVoice = original.resume
  api.session = original.detail
  api.models = original.models
  api.attachments = original.attachments
  api.pins = original.pins
  restoreOutputs()
  restoreOutputs = () => {}
  restoreRoomMocks.splice(0).forEach(restore => restore())
  window.happyDOM.close()
})

test('page loads exact thread identity and context without starting a call or requesting a token', async () => {
  let starts = 0
  let tokens = 0
  api.startRealtimeVoice = async () => {
    starts++
    return session
  }
  api.realtimeVoiceToken = async () => {
    tokens++
    throw new Error('No transport')
  }
  await mountVoice()
  expect(container.textContent).toContain('helper')
  expect(container.textContent).toContain('Exact title chat-1')
  expect(container.textContent).toContain('ID: chat-1')
  expect(container.textContent).toContain('Owner: owner')
  expect(container.textContent).toContain('selected thread')
  expect(starts).toBe(0)
  expect(tokens).toBe(0)
  expect(container.querySelector('[aria-label="Audio output"]')?.textContent).not.toContain(
    'Checking browser audio output controls',
  )
  expect(container.querySelector('[aria-label="Audio output"]')?.textContent).toContain(
    'become available when you start or resume',
  )
  expect(container.querySelector('dialog')).toBeNull()
  expect(link('← Return to thread').getAttribute('href')).toBe('/s/chat-1')
})

test('restricted page is honest and cannot start a microphone', async () => {
  api.realtimeVoiceConfig = async () => ({ enabled: false })
  api.realtimeVoiceSessions = async () => []
  await mountVoice()
  expect(button('Start call').disabled).toBe(true)
  expect(container.textContent).toContain('unavailable or restricted')
})

test('unsupported delegation reasons and closing-boundary warnings are visible without opening details', async () => {
  const unavailable = 'This thread uses a subscription engine that cannot enforce the request budget.'
  api.realtimeVoiceSessions = async () => [
    {
      ...session,
      standby_state: 'closing',
      briefing: {
        context: '',
        restriction: `Restricted read-only mode. ${unavailable}`,
        reason: unavailable,
        delegation_available: false,
      },
    },
  ]
  await mountVoice()
  const reason = [...container.querySelectorAll('p')].find(node => node.textContent === unavailable)
  expect(reason).toBeDefined()
  expect(reason?.closest('details:not([open])')).toBeNull()
  const warning = [...container.querySelectorAll('p')].find(node => node.textContent?.includes('speech may be cut off'))
  expect(warning).toBeDefined()
  expect(warning?.closest('details:not([open])')).toBeNull()
  expect(container.textContent).toContain('Disconnecting')
})

test('mismatched selected thread cannot start a call', async () => {
  api.session = async () => thread('somebody-else')
  await mountVoice()
  expect(container.textContent).toContain('identity does not match')
  expect(button('Start call').disabled).toBe(true)
})

test('saved room reconnect requires a user gesture and explicit end removes storage', async () => {
  let starts = 0
  const stop = spyOn(api, 'stop').mockRejectedValue(new Error('Thread stop must not be called'))
  restoreRoomMocks.push(() => stop.mockRestore())
  api.startRealtimeVoice = async () => {
    starts++
    return session
  }
  api.realtimeVoiceSessions = async () => []
  api.endRealtimeVoice = async () => ({ ...session, status: 'ended' })
  window.sessionStorage.setItem('ergonaut.realtimeVoice.chat-1', 'voice-1')
  await mountVoice()
  await act(async () => {
    button('Start call').click()
  })
  expect(starts).toBe(0)
  expect(container.textContent).toContain('Hello')
  expect(container.textContent).toContain('Token unavailable')
  await act(async () => {
    button('End call').click()
  })
  expect(window.sessionStorage.getItem('ergonaut.realtimeVoice.chat-1')).toBeNull()
  expect(stop).not.toHaveBeenCalled()
  expect(container.textContent).toContain('background work continues')
})

test('route/back/page unmount preserves the logical call and blocks implicit cross-thread replacement', async () => {
  let ends = 0
  let starts = 0
  let tokens = 0
  api.endRealtimeVoice = async () => {
    ends++
    return { ...session, status: 'ended' }
  }
  api.startRealtimeVoice = async () => {
    starts++
    return session
  }
  api.realtimeVoiceToken = async () => {
    tokens++
    throw new Error('No transport fixture')
  }
  await mountVoice()
  await act(async () => {
    button('Start call').click()
  })
  await act(async () => {
    link('← Return to thread').click()
  })
  expect(container.textContent).toContain('Thread view')
  expect(link('Return to voice call').getAttribute('href')).toBe('/s/chat-1/voice')
  expect(container.querySelector('[aria-label="Active voice session"]')?.textContent).toContain(
    'Voice session retained · disconnected',
  )
  expect(container.querySelector('[aria-label="Active voice session"]')?.textContent).not.toContain(
    'Voice call continues',
  )
  await act(async () => {
    button('Browser back').click()
  })
  expect(container.textContent).toContain('Exact title chat-1')
  await act(async () => {
    link('Other thread voice').click()
  })
  expect(container.textContent).toContain('already active in thread chat-1')
  expect(button('Start call').disabled).toBe(true)
  expect(ends).toBe(0)
  expect(starts).toBe(0)
  expect(tokens).toBe(1)
})

test('topbar dropdown and mobile options enter the dedicated page, never the composer', async () => {
  await act(async () => {
    root.render(<VoiceHarness initial="/s/chat-1" realChat />)
  })
  expect(container.querySelector('.chat-transcript')).not.toBeNull()
  expect(container.querySelector('.chat-options a')).toBeNull()
  await act(async () => {
    ;(container.querySelector('[data-options-button]') as HTMLButtonElement).click()
  })
  const desktop = container.querySelector('.chat-options a') as HTMLAnchorElement
  expect(desktop.textContent).toContain('Voice call')
  expect(desktop.getAttribute('href')).toBe('/s/chat-1/voice')
  const mobile = container.querySelector('.chat-sheet-actions a') as HTMLAnchorElement
  expect(mobile.getAttribute('href')).toBe('/s/chat-1/voice')
  expect(container.querySelector('.chat-composer a[href$="/voice"]')).toBeNull()
  await act(async () => {
    desktop.click()
  })
  expect(container.querySelector('h1')?.textContent).toBe('Exact title chat-1')
  expect(container.querySelector('.chat-transcript')).toBeNull()
})

test('mobile options sheet navigates to voice without starting capture', async () => {
  let tokens = 0
  api.realtimeVoiceToken = async () => {
    tokens++
    throw new Error('No transport')
  }
  await act(async () => {
    root.render(<VoiceHarness initial="/s/chat-1" realChat />)
  })
  await act(async () => {
    ;(container.querySelector('.chat-mobile-summary') as HTMLButtonElement).click()
  })
  expect(container.querySelector('dialog')?.open).toBe(true)
  await act(async () => {
    ;(container.querySelector('.chat-sheet-actions a') as HTMLAnchorElement).click()
  })
  expect(container.querySelector('h1')?.textContent).toBe('Exact title chat-1')
  expect(container.querySelector('dialog')).toBeNull()
  expect(tokens).toBe(0)
})

test('standby page never resumes or requests a token until Resume', async () => {
  api.realtimeVoiceSessions = async () => [{ ...session, standby_state: 'standby' }]
  let resumes = 0
  let tokens = 0
  api.resumeRealtimeVoice = async () => {
    resumes++
    return { ...session, standby_state: 'resuming' }
  }
  api.realtimeVoiceToken = async () => {
    tokens++
    throw new Error('Token unavailable')
  }
  await mountVoice()
  expect(resumes).toBe(0)
  expect(tokens).toBe(0)
  expect(container.textContent).toContain('Provider: closed')
  expect(container.textContent).toContain('Resume manually')
  await act(async () => {
    button('Resume').click()
  })
  expect(resumes).toBe(1)
  expect(tokens).toBe(1)
  expect(container.textContent).toContain('Provider: unknown')
})

test.each(['requested', 'closing'] as const)('pending standby %s does not connect or resume', async state => {
  api.realtimeVoiceSessions = async () => [{ ...session, standby_state: state }]
  await mountVoice()
  expect([...container.querySelectorAll('button')].some(node => node.textContent === 'Resume')).toBe(false)
})

test('standby preserves boundary uncertainty and explicit end reports the unconfirmed boundary', async () => {
  api.realtimeVoiceSessions = async () => [
    { ...session, standby_state: 'requested', state: { standby_audio_uncertain: true } },
  ]
  api.endRealtimeVoice = async () => ({ ...session, status: 'ended', standby_state: 'requested' })
  await mountVoice()
  await act(async () => {
    button('End call').click()
  })
  expect(container.textContent).toContain('ended before standby completed')
})

test('resume is single flight and a refused resume remains in standby', async () => {
  api.realtimeVoiceSessions = async () => [{ ...session, standby_state: 'standby' }]
  const { promise, reject } = Promise.withResolvers<RealtimeVoiceSession>()
  let calls = 0
  api.resumeRealtimeVoice = () => {
    calls++
    return promise
  }
  await mountVoice()
  await act(async () => {
    button('Resume').click()
    button('Resume').click()
  })
  expect(calls).toBe(1)
  await act(async () => {
    reject(new Error('Not safe'))
  })
  expect(container.textContent).toContain('Not safe')
  expect(container.textContent).toContain('In standby')
})

test('observed SDK states stay separate from queued work and absent SDK observations remain unknown', async () => {
  api.realtimeVoiceSessions = async () => [
    { ...session, delegations: [{ event_id: 'task', text: 'Lookup', status: 'queued', result: '', error: '' }] },
  ]
  await mountVoice()
  expect(container.textContent).toContain('You: unknown · Assistant: unknown')
  expect(container.textContent).toContain('Queued — may not have started')
  expect(container.textContent).not.toContain('Assistant: thinking')
})

test('SDK labels without an observation timestamp cannot claim speaking or thinking', async () => {
  api.realtimeVoiceSessions = async () => [
    { ...session, runtime: { user_state: 'speaking', agent_state: 'thinking', observed_at: null } },
  ]
  await mountVoice()
  expect(container.textContent).toContain('You: unknown · Assistant: unknown')
})

test('disconnected transport cannot present old SDK observations as current activity', async () => {
  api.realtimeVoiceSessions = async () => [
    { ...session, runtime: { user_state: 'listening', agent_state: 'speaking', observed_at: '2026-01-01T00:00:00Z' } },
  ]
  await mountVoice()
  expect(container.textContent).toContain('Last observed voice SDK · You: unknown · Assistant: unknown')
})

test('partial and final update the same bubble; manual scroll stays put until jump to latest', async () => {
  const draft = {
    turn_id: 'turn-1',
    role: 'assistant' as const,
    text: ' First words ',
    is_final: false,
    delivery: 'unknown',
    created_at: '',
  }
  await act(async () => {
    root.render(<VoiceTranscript voice={{ ...session, transcript: [draft] }} />)
  })
  const bubble = container.querySelector('[data-turn-id="turn-1"]')!
  const scroller = container.querySelector('[data-voice-transcript]') as HTMLDivElement
  Object.defineProperties(scroller, {
    scrollHeight: { configurable: true, value: 1200 },
    clientHeight: { configurable: true, value: 300 },
  })
  scroller.scrollTop = 100
  act(() => {
    scroller.dispatchEvent(new window.Event('scroll'))
  })
  await act(async () => {
    root.render(
      <VoiceTranscript
        voice={{ ...session, transcript: [{ ...draft, text: 'First words completed.', is_final: true }, draft] }}
      />,
    )
  })
  expect(container.querySelector('[data-turn-id="turn-1"]')).toBe(bubble)
  expect(container.querySelectorAll('[data-turn-id]')).toHaveLength(1)
  expect(bubble.textContent).toContain('First words completed.')
  expect(bubble.getAttribute('data-final')).toBe('true')
  expect(scroller.scrollTop).toBe(100)
  await act(async () => {
    button('Jump to latest').click()
  })
  expect(scroller.scrollTop).toBe(1200)
  await act(async () => {
    root.render(<VoiceTranscript voice={{ ...session, transcript: [{ ...draft, text: 'Another partial' }] }} />)
  })
  expect(scroller.scrollTop).toBe(1200)
})

test('active page polling progressively replaces a draft without opening a transport', async () => {
  const draft = {
    turn_id: 'progressive',
    role: 'assistant' as const,
    text: 'Working words',
    is_final: false,
    delivery: 'unknown',
    created_at: '',
  }
  let observed: RealtimeVoiceSession = { ...session, transcript: [draft] }
  let tokens = 0
  api.realtimeVoiceSessions = async () => [observed]
  api.realtimeVoiceSession = async () => observed
  api.realtimeVoiceToken = async () => {
    tokens++
    throw new Error('No transport')
  }
  await mountVoice()
  const bubble = container.querySelector('[data-turn-id="progressive"]')
  observed = { ...session, transcript: [{ ...draft, text: 'Completed words', is_final: true }] }
  const { promise, resolve } = Promise.withResolvers<void>()
  setTimeout(resolve, 850)
  await act(async () => {
    await promise
  })
  expect(container.querySelector('[data-turn-id="progressive"]')).toBe(bubble)
  expect(bubble?.textContent).toContain('Completed words')
  expect(container.querySelectorAll('[data-turn-id="progressive"]')).toHaveLength(1)
  expect(tokens).toBe(0)
})

test('a single durable card shows the stage and transcript without claiming playback', () => {
  const html = renderToStaticMarkup(
    createElement(VoiceSessionCard, {
      session: {
        ...session,
        status: 'ended',
        state: { current_step: 'Research', decisions: ['Use blue'], constraints: ['No deployment'] },
        delegations: [
          { event_id: 'task-1', text: 'Find sources', status: 'answered', result: 'Found sources', error: '' },
        ],
        events: [
          ...session.events,
          { event_id: 'event-2', kind: 'assistant', text: 'Working on it', delivery: 'unknown', created_at: '' },
          { event_id: 'event-3', kind: 'assistant', text: 'Interrupted', delivery: 'interrupted', created_at: '' },
          { event_id: 'event-4', kind: 'assistant', text: 'Played', delivery: 'delivered', created_at: '' },
        ],
      },
    }),
  )
  expect(html.match(/aria-label=\"Voice session\"/g)).toHaveLength(1)
  for (const text of [
    'Ended',
    'Research',
    'Use blue',
    'No deployment',
    'Find sources',
    'Found sources',
    'Hello',
    'Working on it',
    'playback unconfirmed',
    '(interrupted)',
    'playback confirmed; hearing not established',
  ]) {
    expect(html).toContain(text)
  }
})

test.each([{ delegations: undefined }, { delegations: [] }])(
  'assistant history claims without delegation records remain unverified (%j)',
  ({ delegations }) => {
    act(() => {
      root.render(
        createElement(VoiceSessionCard, {
          session: {
            ...session,
            delegations,
            state: { current_step: 'History checked', completed_steps: ['History lookup'] },
            events: [
              {
                event_id: 'claim',
                kind: 'assistant',
                text: 'I checked your history and found the answer.',
                delivery: 'delivered',
                created_at: '',
              },
            ],
          },
        }),
      )
    })
    const background = container.querySelector('[aria-label="Background work"]')
    expect(background?.textContent).toContain('No background request submitted; no verified history lookup result.')
    expect(container.querySelector('[aria-label="Durable background result"]')).toBeNull()
    expect(container.querySelector('[aria-label="Stage"]')?.textContent).toContain('reported, unverified')
    const transcript = container.querySelector('details')
    expect(transcript?.textContent).toContain('I checked your history and found the answer.')
    expect(transcript?.textContent).toContain('Assistant speech is generated and unverified as work evidence.')
    expect(transcript?.textContent).toContain('playback confirmed; hearing not established')
  },
)

test('a queued delegation does not imply work started or expose a result as evidence', () => {
  act(() => {
    root.render(
      createElement(VoiceSessionCard, {
        session: {
          ...session,
          delegations: [
            { event_id: 'task-1', text: 'Look up history', status: 'queued', result: 'Not an answer yet', error: '' },
          ],
        },
      }),
    )
  })
  const background = container.querySelector('[aria-label="Background work"]')
  expect(background?.textContent).toContain('Look up history')
  expect(background?.textContent).toContain('Queued — may not have started; no verified result.')
  expect(background?.textContent).not.toContain('Not an answer yet')
  expect(background?.textContent).not.toContain('No background request submitted')
  expect(container.querySelector('[aria-label="Durable background result"]')).toBeNull()
})

test('only an answered durable delegation exposes its result as background evidence', () => {
  act(() => {
    root.render(
      createElement(VoiceSessionCard, {
        session: {
          ...session,
          delegations: [
            {
              event_id: 'task-1',
              text: 'Look up history',
              status: 'answered',
              result: 'The previous discussion chose blue.',
              error: '',
            },
          ],
        },
      }),
    )
  })
  const background = container.querySelector('[aria-label="Background work"]')
  expect(background?.textContent).toContain('Answered — durable background result recorded.')
  expect(background?.querySelector('[aria-label="Durable background result"]')?.textContent).toBe(
    'The previous discussion chose blue.',
  )
  expect(background?.textContent).not.toContain('No background request submitted')
})

test.each(['failed', 'error', 'answered'])(
  'a delegation with status %s and an error is not result evidence',
  status => {
    act(() => {
      root.render(
        createElement(VoiceSessionCard, {
          session: {
            ...session,
            delegations: [
              {
                event_id: 'task-1',
                text: 'Look up history',
                status,
                result: 'An incomplete answer',
                error: 'History lookup failed',
              },
            ],
          },
        }),
      )
    })
    const background = container.querySelector('[aria-label="Background work"]')
    expect(background?.textContent).toContain('Failed — no verified result.')
    expect(background?.textContent).toContain('History lookup failed')
    expect(background?.textContent).not.toContain('An incomplete answer')
    expect(container.querySelector('[aria-label="Durable background result"]')).toBeNull()
  },
)

// These fixtures are silent: neither output detection nor a rejected token
// constructs a room, plays media or requests a microphone.
function installOutputs() {
  const prototype = window.HTMLMediaElement.prototype
  const previousSink = Object.getOwnPropertyDescriptor(prototype, 'sinkId')
  const previousMethod = Object.getOwnPropertyDescriptor(prototype, 'setSinkId')
  const previousDevices = Object.getOwnPropertyDescriptor(window.navigator, 'mediaDevices')
  const sinks = new WeakMap<object, string>()
  const listeners = new Set<() => void>()
  const routing = {
    rejected: null as string | null,
    captures: 0,
    outputs: [
      { kind: 'audiooutput', deviceId: 'bluetooth', label: 'Bluetooth headset' },
      { kind: 'audiooutput', deviceId: 'speaker', label: 'Speakers' },
    ],
    async enumerateDevices() {
      return routing.outputs
    },
    async getUserMedia() {
      routing.captures++
      throw new Error('No capture allowed')
    },
    addEventListener(_event: string, listener: () => void) {
      listeners.add(listener)
    },
    removeEventListener(_event: string, listener: () => void) {
      listeners.delete(listener)
    },
    change() {
      listeners.forEach(listener => listener())
    },
    listeners,
  }
  Object.defineProperties(prototype, {
    sinkId: {
      configurable: true,
      get() {
        return sinks.get(this) ?? ''
      },
    },
    setSinkId: {
      configurable: true,
      async value(deviceId: string) {
        if (routing.rejected === deviceId) throw new DOMException('Blocked', 'NotAllowedError')
        sinks.set(this, deviceId)
      },
    },
  })
  Object.defineProperty(window.navigator, 'mediaDevices', { configurable: true, value: routing })
  restoreOutputs = () => {
    if (previousSink) Object.defineProperty(prototype, 'sinkId', previousSink)
    else Reflect.deleteProperty(prototype, 'sinkId')
    if (previousMethod) Object.defineProperty(prototype, 'setSinkId', previousMethod)
    else Reflect.deleteProperty(prototype, 'setSinkId')
    if (previousDevices) Object.defineProperty(window.navigator, 'mediaDevices', previousDevices)
    else Reflect.deleteProperty(window.navigator, 'mediaDevices')
  }
  return routing
}

async function openOutputPanel() {
  api.realtimeVoiceConfig = async () => ({ enabled: true })
  api.realtimeVoiceSessions = async () => [session]
  api.realtimeVoiceToken = async () => {
    throw new Error('No room connection in this fixture')
  }
  await mountVoice()
  await act(async () => {
    button('Start call').click()
  })
}

test('unsupported/unknown browser APIs show OS guidance instead of a cosmetic output toggle', async () => {
  const routing = installOutputs()
  Object.defineProperty(window.HTMLMediaElement.prototype, 'setSinkId', { configurable: true, value: undefined })
  await openOutputPanel()
  expect(container.querySelector('select')).toBeNull()
  expect(container.textContent).toContain('phone or operating system')
  expect(container.textContent).toContain('cannot confirm the physical route')
  expect(routing.captures).toBe(0)
})

test('the real selector shows the applied device and preserves it after permission rejection', async () => {
  const routing = installOutputs()
  await openOutputPanel()
  const select = container.querySelector('select')!
  expect(select.value).toBe('')
  expect(select.textContent).toContain('Bluetooth headset')
  await act(async () => {
    select.value = 'bluetooth'
    select.dispatchEvent(new window.Event('change', { bubbles: true }))
  })
  expect(select.value).toBe('bluetooth')
  routing.rejected = 'speaker'
  await act(async () => {
    select.value = 'speaker'
    select.dispatchEvent(new window.Event('change', { bubbles: true }))
  })
  expect(select.value).toBe('bluetooth')
  expect(container.textContent).toContain('permission was denied or blocked')
  expect(container.textContent).toContain('hearing and the physical audio route are not confirmed')
  expect(container.textContent).toContain('Microphone control is separate')
  expect(routing.captures).toBe(0)
})

test('a denied capability probe hides the selector and remains phone-controlled', async () => {
  const routing = installOutputs()
  routing.rejected = ''
  await openOutputPanel()
  expect(container.querySelector('select')).toBeNull()
  expect(container.textContent).toContain('permission was denied or blocked')
  expect(container.textContent).toContain('phone or operating system')
  expect(routing.captures).toBe(0)
})

test('device loss updates outputs and navigation keeps the app-owned routing listeners', async () => {
  const routing = installOutputs()
  await openOutputPanel()
  await act(async () => {
    const select = container.querySelector('select')!
    select.value = 'bluetooth'
    select.dispatchEvent(new window.Event('change', { bubbles: true }))
  })
  expect(routing.listeners.size).toBe(1)
  await act(async () => {
    routing.outputs = []
    routing.change()
  })
  expect(container.querySelector('select')?.value).toBe('')
  expect(container.textContent).toContain('no longer available')
  await act(async () => {
    link('← Return to thread').click()
  })
  expect(routing.listeners.size).toBe(1)
  await act(async () => {
    link('Return to voice call').click()
  })
  expect(container.querySelector('select')?.value).toBe('')
  expect(routing.listeners.size).toBe(1)
  expect(routing.captures).toBe(0)
})

test('optional restricted-mode briefing is upfront even when the voice token is unavailable', async () => {
  installOutputs()
  api.realtimeVoiceConfig = async () => ({ enabled: true })
  const restriction = 'Restricted mode: one read-only gpt-6-luna background request; no write actions.'
  api.realtimeVoiceSessions = async () => [
    { ...session, briefing: { context: 'Current thread', restriction, delegation_available: true } },
  ]
  api.realtimeVoiceToken = async () => {
    throw new Error('Token unavailable')
  }
  await mountVoice()
  await act(async () => {
    button('Start call').click()
  })
  expect(container.querySelector('[role="status"]')?.textContent).toContain(restriction)
  expect(container.textContent!.indexOf(restriction)).toBeLessThan(container.textContent!.indexOf('Hello'))
})

test('mocked room events reapply outputs through reconnect and new-room standby resume without capture', async () => {
  const routing = installOutputs()
  const rooms: Room[] = []
  let disconnects = 0
  let ends = 0
  api.endRealtimeVoice = async () => {
    ends++
    return { ...session, status: 'ended' }
  }
  const originalOn = Room.prototype.on
  // connect/disconnect are instance fields in LiveKit 2.23.0. Replace them
  // when the component registers its first handler, before it can connect.
  const observe = spyOn(Room.prototype, 'on').mockImplementation(function (this: Room, event, listener) {
    if (event === RoomEvent.TrackSubscribed) {
      this.connect = async () => {
        rooms.push(this)
      }
      this.disconnect = async () => {
        disconnects++
      }
    }
    return originalOn.call(this, event, listener)
  })
  const microphone = spyOn(LocalParticipant.prototype, 'setMicrophoneEnabled').mockResolvedValue(undefined)
  restoreRoomMocks.push(
    () => observe.mockRestore(),
    () => microphone.mockRestore(),
  )
  api.realtimeVoiceConfig = async () => ({ enabled: true, standby_enabled: true })
  const live = {
    ...session,
    standby_state: 'live' as const,
    runtime: { user_state: 'listening', agent_state: 'speaking', observed_at: '2026-01-01T00:00:00Z' },
  }
  api.realtimeVoiceSessions = async () => [live]
  api.realtimeVoiceSession = async () => live
  api.realtimeVoiceToken = async () => ({ url: 'wss://never-joined.invalid', token: 'not-a-real-token' })
  api.standbyRealtimeVoice = async () => ({ ...live, standby_state: 'standby' })
  api.resumeRealtimeVoice = async () => ({ ...live, standby_state: 'resuming' })
  await mountVoice()
  await act(async () => {
    button('Start call').click()
  })
  expect(rooms).toHaveLength(1)
  expect(container.textContent).toContain('Last observed voice SDK · You: listening · Assistant: speaking')
  expect(container.textContent).toContain('not a bridge heartbeat')
  await act(async () => {
    link('← Return to thread').click()
  })
  expect(disconnects).toBe(0)
  expect(ends).toBe(0)
  expect(container.querySelector('[aria-label="Active voice session"]')?.textContent).toContain(
    'Voice call continues · Microphone: enabled',
  )
  await act(async () => {
    button('Browser back').click()
  })
  expect(rooms).toHaveLength(1)
  expect(microphone).toHaveBeenCalledTimes(1)
  let firstAudio: HTMLMediaElement
  const firstTrack = {
    kind: Track.Kind.Audio,
    attach(element: HTMLMediaElement) {
      firstAudio = element
      return element
    },
    detach: () => (firstAudio ? [firstAudio] : []),
  }
  await act(async () => {
    rooms[0].emit(RoomEvent.TrackSubscribed, firstTrack)
    const select = container.querySelector('select')!
    select.value = 'bluetooth'
    select.dispatchEvent(new window.Event('change', { bubbles: true }))
  })
  expect(firstAudio.sinkId).toBe('bluetooth')
  await act(async () => {
    rooms[0].emit(RoomEvent.Reconnecting)
    await firstAudio.setSinkId('')
    rooms[0].emit(RoomEvent.Reconnected)
  })
  expect(firstAudio.sinkId).toBe('bluetooth')
  await act(async () => {
    button('Standby while waiting').click()
  })
  expect(firstAudio.isConnected).toBe(false)
  await act(async () => {
    button('Resume').click()
  })
  expect(rooms).toHaveLength(2)
  let secondAudio: HTMLMediaElement
  const secondTrack = {
    kind: Track.Kind.Audio,
    attach(element: HTMLMediaElement) {
      expect(element.sinkId).toBe('bluetooth') // routed before any stream is attached/played
      secondAudio = element
      return element
    },
    detach: () => (secondAudio ? [secondAudio] : []),
  }
  await act(async () => {
    rooms[1].emit(RoomEvent.TrackSubscribed, secondTrack)
    rooms[0].emit(RoomEvent.TrackSubscribed, firstTrack) // an obsolete room must not attach playback
  })
  expect(secondAudio.sinkId).toBe('bluetooth')
  expect(firstAudio.isConnected).toBe(false)
  expect(container.querySelector('select')?.value).toBe('bluetooth')
  expect(microphone).toHaveBeenCalledTimes(2) // only the existing explicit connect/resume path
  let cancelledAttachments = 0
  const cancelledTrack = {
    kind: Track.Kind.Audio,
    attach(element: HTMLMediaElement) {
      cancelledAttachments++
      return element
    },
    detach: () => [],
  }
  await act(async () => {
    rooms[1].emit(RoomEvent.TrackSubscribed, cancelledTrack)
    rooms[1].emit(RoomEvent.TrackUnsubscribed, cancelledTrack)
  })
  expect(cancelledAttachments).toBe(0)
  expect(routing.captures).toBe(0)
})
