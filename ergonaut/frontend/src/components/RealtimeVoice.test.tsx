import { afterEach, beforeEach, expect, spyOn, test } from 'bun:test'
import { act, createElement } from 'react'
import type { createRoot as CreateRoot, Root } from 'react-dom/client'
import { renderToStaticMarkup } from 'react-dom/server'
import { Window } from 'happy-dom'
import {
  LocalAudioTrack,
  LocalParticipant,
  RemoteAudioTrack,
  Room,
  RoomEvent,
  Track,
  TrackEvent,
  type LocalTrackPublication,
} from 'livekit-client'
import { Link, MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom'
import { api, type ClientAudioReport, type RealtimeVoiceSession, type SessionDetail } from '../api'
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
  reportAudio: api.reportRealtimeVoiceAudio,
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
let reports: { sessionId: string; report: ClientAudioReport }[] = []

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
  reports = []
  api.reportRealtimeVoiceAudio = async (sessionId, report) => {
    reports.push({ sessionId, report })
    return { recorded: true }
  }
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
  api.reportRealtimeVoiceAudio = original.reportAudio
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
  const mobile = container.querySelector('.chat-sheet-options a') as HTMLAnchorElement
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
    ;(container.querySelector('.chat-sheet-options a') as HTMLAnchorElement).click()
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
    {
      ...session,
      request_budget: {
        limit: 3,
        used: 1,
        remaining: 2,
        collecting: false,
        inflight: true,
        request_ids: ['task'],
      },
      delegations: [{ event_id: 'task', text: 'Lookup', status: 'queued', result: '', error: '' }],
    },
  ]
  await mountVoice()
  expect(container.textContent).toContain('You: unknown · Assistant: unknown')
  expect(container.textContent).toContain('Queued — may not have started')
  const backgroundSummary = [...container.querySelectorAll('summary')].find(node =>
    node.textContent?.includes('Background work'),
  )
  expect(backgroundSummary?.textContent).toContain('2 of 3 requests remaining')
  expect(container.querySelector('[aria-label="Voice request allowance"]')?.textContent).toContain(
    'A request is pending',
  )
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

function turn(id: string, role: 'user' | 'assistant', text: string, extra: Record<string, unknown> = {}) {
  return { turn_id: id, role, text, is_final: true, delivery: 'unknown', created_at: '', ...extra }
}
async function renderTurns(transcript: ReturnType<typeof turn>[]) {
  await act(async () => {
    root.render(<VoiceTranscript voice={{ ...session, transcript }} />)
  })
  return [...container.querySelectorAll('[data-turn-id]')].map(node => node.textContent ?? '')
}

test('assistant fragments of one speaker run share one bubble without playback labels', async () => {
  const bubbles = await renderTurns([
    turn('a1', 'assistant', 'three-request allowance.', { created_at: '2026-01-01T00:00:01Z', segment: 'c1' }),
    turn('a2', 'assistant', 'That', { created_at: '2026-01-01T00:00:02Z', segment: 'c1' }),
    turn('a3', 'assistant', 'earlier playback issue was just volume.', {
      created_at: '2026-01-01T00:00:03Z',
      segment: 'c1',
    }),
    turn('a4', 'assistant', 'We', { is_final: false, created_at: '2026-01-01T00:00:04Z', segment: 'c1' }),
  ])
  expect(bubbles).toHaveLength(1)
  expect(bubbles[0]).toContain('three-request allowance. That earlier playback issue was just volume. We')
  expect(bubbles[0]).toContain('live transcript')
  expect(container.textContent).not.toMatch(/Playback (unconfirmed|confirmed)/)
})

test('duplicate and out-of-order events neither repeat text nor reorder the call', async () => {
  const user = turn('u1', 'user', 'Question', { created_at: '2026-01-01T00:00:00Z', segment: 'c1' })
  const first = turn('a1', 'assistant', 'One', { created_at: '2026-01-01T00:00:01Z', segment: 'c1' })
  const second = turn('a2', 'assistant', 'two.', { created_at: '2026-01-01T00:00:02Z', segment: 'c1' })
  const bubbles = await renderTurns([
    second,
    { ...first, is_final: false, text: 'On' },
    user,
    first,
    first,
    { ...second, is_final: false, text: 'tw' },
  ])
  expect(bubbles).toHaveLength(2)
  expect(bubbles[0]).toContain('Question')
  expect(bubbles[1]).toContain('One two.')
  expect(bubbles[1]).not.toContain('One One')
  expect(container.querySelector('[data-turn-id="u1"]')!.getAttribute('data-final')).toBe('true')
})

test('speaker changes, interruptions and reconnects break bubbles; interruption is labeled', async () => {
  const bubbles = await renderTurns([
    turn('a1', 'assistant', 'Hello', { created_at: '2026-01-01T00:00:01Z', segment: 'c1' }),
    turn('a2', 'assistant', 'there and', {
      created_at: '2026-01-01T00:00:02Z',
      segment: 'c1',
      delivery: 'interrupted',
    }),
    turn('a3', 'assistant', 'After interrupt', { created_at: '2026-01-01T00:00:03Z', segment: 'c1' }),
    turn('u1', 'user', 'Hey', { created_at: '2026-01-01T00:00:04Z', segment: 'c1' }),
    turn('u2', 'user', 'again', { created_at: '2026-01-01T00:00:05Z', segment: 'c2' }),
    turn('a4', 'assistant', 'New call', { created_at: '2026-01-01T00:00:06Z', segment: 'c2' }),
    turn('a5', 'assistant', 'continues', { created_at: '2026-01-01T00:00:07Z', segment: 'c3' }),
  ])
  expect(bubbles.map(text => text.replace(/^(You|Assistant)/, '$1|'))).toEqual([
    expect.stringContaining('Hello there and'),
    expect.stringContaining('After interrupt'),
    expect.stringContaining('Hey'),
    expect.stringContaining('again'),
    expect.stringContaining('New call'),
    expect.stringContaining('continues'),
  ])
  expect(bubbles[0]).toContain('Interrupted')
  expect(bubbles[1]).not.toContain('Interrupted')
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
  // The bridge can finish an already-accepted turn after End's first snapshot.
  api.endRealtimeVoice = async () => ({ ...session, status: 'ended', transcript: [] })
  await act(async () => {
    button('End call').click()
  })
  observed = {
    ...session,
    status: 'ended',
    transcript: [{ ...draft, text: 'Final accepted before End', is_final: true }],
  }
  const finalWait = Promise.withResolvers<void>()
  setTimeout(finalWait.resolve, 850)
  await act(async () => {
    await finalWait.promise
  })
  expect(container.querySelector('[data-turn-id="progressive"]')?.textContent).toContain('Final accepted before End')
  expect(tokens).toBe(0)
})

test('normal approval-waiting child work stays observed after End and navigation without reopening voice', async () => {
  let observed: RealtimeVoiceSession = {
    ...session,
    delegations: [{ event_id: 'original', text: 'Follow child', status: 'answered', result: 'Submitted', error: '' }],
    thread_updates: [
      {
        event_id: 'original',
        update_id: 'child:waiting',
        chat_id: 'chat-1',
        text: 'Existing child task',
        status: 'waiting',
        result: '',
        error: '',
      },
    ],
  }
  let tokens = 0
  api.realtimeVoiceSessions = async () => [observed]
  api.realtimeVoiceSession = async () => observed
  api.realtimeVoiceToken = async () => {
    tokens++
    throw new Error('No room authorized')
  }
  api.endRealtimeVoice = async () => (observed = { ...observed, status: 'ended' })
  await mountVoice()
  expect(container.textContent).toContain('Waiting for normal thread approval')
  await act(async () => {
    button('End call').click()
  })
  await act(async () => {
    link('← Return to thread').click()
  })
  observed = {
    ...observed,
    thread_updates: [
      {
        event_id: 'original',
        update_id: 'child:answered',
        chat_id: 'chat-1',
        text: 'Existing child task',
        status: 'answered',
        result: 'Durable child completion',
        error: '',
      },
    ],
  }
  await act(async () => {
    const { promise, resolve } = Promise.withResolvers<void>()
    setTimeout(resolve, 850)
    await promise
  })
  await act(async () => {
    link('Voice fixture').click()
  })
  expect(container.textContent).toContain('Durable child completion')
  expect(container.textContent).toContain('Call ended')
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

test('sequential request allowance follows durable work without implying execution or starting another request', () => {
  const task = { event_id: 'task-1', text: 'Look up history', status: 'queued', result: 'Not an answer yet', error: '' }
  const budget = { limit: 3, used: 1, remaining: 2, collecting: false, inflight: true, request_ids: ['task-1'] }
  function renderWorkflow(update: Partial<RealtimeVoiceSession> = {}) {
    act(() => {
      root.render(
        <MemoryRouter>
          <VoiceSessionCard session={{ ...session, request_budget: budget, delegations: [task], ...update }} />
        </MemoryRouter>,
      )
    })
  }
  renderWorkflow()
  const background = container.querySelector('[aria-label="Background work"]')
  expect(background?.textContent).toContain('Look up history')
  expect(background?.textContent).toContain('Queued — may not have started; no verified result.')
  expect(background?.textContent).not.toContain('Not an answer yet')
  expect(background?.textContent).not.toContain('No background request submitted')
  expect(container.querySelector('[aria-label="Durable background result"]')).toBeNull()
  expect(background?.textContent).toContain('2 of 3 remaining')
  expect(background?.textContent).toContain('A request is pending')
  expect(background?.querySelector('a')).toBeNull()

  renderWorkflow({ delegations: [{ ...task, status: 'delivered' }] })
  expect(background?.textContent).toContain('Accepted by worker — execution not confirmed')
  expect(background?.textContent).toContain('A request is pending')
  expect(container.querySelector('[aria-label="Durable background result"]')).toBeNull()

  renderWorkflow({
    delegations: [{ ...task, status: 'answered', result: 'Verified stored answer' }],
    request_budget: { ...budget, inflight: false },
  })
  expect(background?.textContent).not.toContain('A request is pending')
  expect(background?.textContent).toContain('2 of 3 remaining')
  expect(background?.querySelector('[aria-label="Durable background result"]')?.textContent).toBe(
    'Verified stored answer',
  )

  renderWorkflow({
    request_budget: { ...budget, used: 3, remaining: 0, request_ids: ['task-1', 'task-2', 'task-3'] },
  })
  expect(background?.textContent).toContain('0 of 3 remaining')
  expect(background?.textContent).toContain('A request is pending')
  expect(background?.textContent).toContain('Voice request allowance exhausted')
  expect(background?.querySelector('a')?.getAttribute('href')).toBe('/s/chat-1')
  expect(background?.querySelector('button')).toBeNull()
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
    'Voice call continues · Microphone: disabled',
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
  let cancelledAudio: HTMLMediaElement | undefined
  const cancelledTrack = {
    kind: Track.Kind.Audio,
    attach(element: HTMLMediaElement) {
      cancelledAudio = element
      return element
    },
    detach: () => [],
  }
  await act(async () => {
    rooms[1].emit(RoomEvent.TrackSubscribed, cancelledTrack)
    rooms[1].emit(RoomEvent.TrackUnsubscribed, cancelledTrack)
  })
  expect(cancelledAudio?.isConnected).toBe(false) // unsubscribed playback leaves no element behind
  expect(routing.captures).toBe(0)
})

function pause(ms: number) {
  const { promise, resolve } = Promise.withResolvers<void>()
  setTimeout(resolve, ms)
  return act(async () => {
    await promise
  })
}

// A subscribed assistant track; `attach` mirrors LiveKit's: it binds the element the page passes in.
function assistantTrack(sid = 'TR_assistant') {
  const track = {
    kind: Track.Kind.Audio,
    sid,
    element: undefined as HTMLMediaElement | undefined,
    failed: undefined as ((error: unknown) => void) | undefined,
    attach(element: HTMLMediaElement) {
      this.element = element
      return element
    },
    detach() {
      return this.element ? [this.element] : []
    },
    on(event: string, listener: (error: unknown) => void) {
      if (event === TrackEvent.AudioPlaybackFailed) this.failed = listener
      return this
    },
  }
  return track
}

async function connectCall(initialMicrophoneError?: Error) {
  const rooms: Room[] = []
  const originalOn = Room.prototype.on
  const observe = spyOn(Room.prototype, 'on').mockImplementation(function (this: Room, event, listener) {
    if (event === RoomEvent.TrackSubscribed) {
      this.connect = async () => {
        rooms.push(this)
      }
      this.disconnect = async () => {}
    }
    return originalOn.call(this, event, listener)
  })
  const microphone = spyOn(LocalParticipant.prototype, 'setMicrophoneEnabled').mockResolvedValue(undefined)
  if (initialMicrophoneError) microphone.mockRejectedValueOnce(initialMicrophoneError)
  restoreRoomMocks.push(
    () => observe.mockRestore(),
    () => microphone.mockRestore(),
  )
  api.realtimeVoiceToken = async () => ({ url: 'wss://never-joined.invalid', token: 'not-a-real-token' })
  await mountVoice()
  await act(async () => {
    button('Start call').click()
  })
  expect(rooms).toHaveLength(1)
  return rooms[0]
}

const statuses = () => reports.map(item => item.report.status)
const enableSound = () =>
  [...container.querySelectorAll('button')].find(item => item.textContent?.trim() === 'Enable sound')

test('assistant audio plays immediately even when output routing never settles, and Enable sound stays until it plays', async () => {
  installOutputs()
  // A stalled setSinkId: the routing queue never drains.
  Object.defineProperty(window.HTMLMediaElement.prototype, 'setSinkId', {
    configurable: true,
    value: () => new Promise(() => {}),
  })
  api.reportRealtimeVoiceAudio = original.reportAudio
  const fetches: { url: string; init: RequestInit }[] = []
  const fetchSpy = spyOn(globalThis, 'fetch').mockImplementation((async (url: string, init: RequestInit) => {
    fetches.push({ url, init })
    return new Response(JSON.stringify({ recorded: true }))
  }) as typeof fetch)
  restoreRoomMocks.push(() => fetchSpy.mockRestore())
  const room = await connectCall()
  expect(enableSound()).toBeDefined() // connected, nothing confirmed playing yet
  expect(container.textContent).toContain("hasn't started playing in this browser yet")
  const track = assistantTrack()
  await act(async () => {
    room.emit(RoomEvent.TrackSubscribed, track)
  })
  expect(track.element?.isConnected).toBe(true)
  expect(enableSound()).toBeDefined()
  await act(async () => {
    track.element!.dispatchEvent(new window.Event('playing'))
    track.element!.dispatchEvent(new window.Event('playing'))
  })
  expect(enableSound()).toBeUndefined()
  expect(container.textContent).toContain('Browser playback: playing; hearing unconfirmed')
  await act(async () => {
    track.element!.dispatchEvent(new window.Event('pause'))
  })
  expect(enableSound()).toBeDefined() // a paused element is no longer confirmed playback
  await act(async () => {
    track.element!.dispatchEvent(new window.Event('playing'))
  })
  const sent = fetches.map(item => ({ url: item.url, body: JSON.parse(String(item.init.body)) }))
  expect(sent.map(item => item.body.status)).toEqual(['track_subscribed', 'playback_started'])
  for (const item of sent) {
    expect(item.url).toBe('/api/realtime-voice/sessions/voice-1/client-audio')
    expect(item.body.event_id).toMatch(/^[A-Za-z0-9_-]{1,80}$/)
  }
  expect(fetches[0].init.method).toBe('POST')
  expect(new Set(sent.map(item => item.body.event_id)).size).toBe(2)
})

test('blocked playback keeps Enable sound, reports once, and Enable sound unlocks inside the click', async () => {
  const room = await connectCall()
  const track = assistantTrack()
  await act(async () => {
    room.emit(RoomEvent.TrackSubscribed, track)
    track.failed?.(new DOMException('Autoplay refused', 'NotAllowedError'))
    room.emit(RoomEvent.AudioPlaybackStatusChanged, false)
    room.emit(RoomEvent.AudioPlaybackStatusChanged, false)
  })
  expect(enableSound()).toBeDefined()
  expect(container.textContent).toContain('Your browser blocked playback.')
  expect(container.textContent).toContain('Browser playback: blocked by the browser')
  const blocked = reports.filter(item => item.report.status === 'playback_blocked')
  expect(blocked).toHaveLength(1)
  expect(blocked[0].report.detail).toBe('NotAllowedError')
  let unlocks = 0
  room.startAudio = async () => {
    unlocks++
    if (unlocks === 1) throw new DOMException('Still refused', 'NotAllowedError')
  }
  // The browser refuses the element too until a gesture it accepts.
  track.element!.play = async () => {
    throw new DOMException('Still refused', 'NotAllowedError')
  }
  await act(async () => {
    enableSound()!.click()
  })
  expect(unlocks).toBe(1)
  expect(enableSound()).toBeDefined()
  expect(container.textContent).toContain('Still refused')
  expect(statuses()).not.toContain('sound_enabled')
  Reflect.deleteProperty(track.element!, 'play')
  await act(async () => {
    enableSound()!.click()
  })
  expect(unlocks).toBe(2)
  expect(track.element?.paused).toBe(false) // the click also played the attached element directly
  expect(enableSound()).toBeUndefined()
  expect(statuses().filter(status => status === 'sound_enabled')).toHaveLength(1)
  expect(statuses().filter(status => status === 'playback_blocked')).toHaveLength(1)
})

test('a new final assistant turn without playback shows an alert and reports turn_unplayed once', async () => {
  const spoken = (turn_id: string) => ({
    turn_id,
    role: 'assistant' as const,
    text: `Said ${turn_id}`,
    is_final: true,
    delivery: 'unknown',
    created_at: '2026-01-01T00:00:00Z',
  })
  let observed: RealtimeVoiceSession = { ...session, transcript: [spoken('before-call')] }
  api.realtimeVoiceSessions = async () => [observed]
  api.realtimeVoiceSession = async () => observed
  const room = await connectCall()
  const track = assistantTrack()
  await act(async () => {
    room.emit(RoomEvent.TrackSubscribed, track)
  })
  expect(container.querySelector('[role="alert"]')).toBeNull() // earlier turns were not spoken to this connection
  observed = { ...session, transcript: [spoken('before-call'), spoken('during-call')] }
  await pause(850)
  expect(container.querySelector('[role="alert"]')?.textContent).toContain(
    'The assistant spoke, but this browser has not started playing its audio. Tap Enable sound.',
  )
  observed = { ...session, transcript: [spoken('before-call'), spoken('during-call'), spoken('again')] }
  await pause(850)
  expect(statuses().filter(status => status === 'turn_unplayed')).toHaveLength(1)
  expect(reports.every(item => item.sessionId === 'voice-1')).toBe(true)
  await act(async () => {
    track.element!.dispatchEvent(new window.Event('playing'))
  })
  expect(container.querySelector('[role="alert"]')).toBeNull()
})

test('rising inbound audio energy is reported once and explains volume when playback is confirmed', async () => {
  const room = await connectCall()
  const energies = [0, 0.4, 0.9]
  let samples = 0
  const track = Object.assign(Object.create(RemoteAudioTrack.prototype) as RemoteAudioTrack, assistantTrack(), {
    _mediaStreamTrack: { enabled: true }, // read by LiveKit's event logging
    getReceiverStats: async () => ({ type: 'audio', totalAudioEnergy: energies[Math.min(samples++, 2)] }),
  })
  await act(async () => {
    room.emit(RoomEvent.TrackSubscribed, track)
    track.element!.dispatchEvent(new window.Event('playing'))
  })
  expect(container.textContent).toContain('Assistant audio arriving: no')
  await pause(2300)
  expect(container.textContent).toContain('Assistant audio arriving: yes')
  expect(container.textContent).toContain('Android uses the call volume')
  const sampled = samples
  await pause(1100)
  expect(samples).toBe(sampled) // polling stops once arrival is known
  expect(statuses().filter(status => status === 'audio_arriving')).toHaveLength(1)
})

test('local microphone ended event clears enabled UI and reconnect observes the current live track', async () => {
  const previousStream = globalThis.MediaStream
  class SyntheticStream {
    constructor(private tracks: MediaStreamTrack[]) {}
    getTracks() {
      return this.tracks
    }
    getAudioTracks() {
      return this.tracks
    }
  }
  Object.assign(globalThis, { MediaStream: SyntheticStream })
  restoreRoomMocks.push(() => {
    globalThis.MediaStream = previousStream
  })
  const native = Object.assign(new window.EventTarget(), {
    id: 'synthetic-input',
    kind: 'audio',
    enabled: true,
    muted: false,
    readyState: 'live',
    getConstraints: () => ({}),
    getSettings: () => ({}),
    stop() {},
  })
  const track = new LocalAudioTrack(native as unknown as MediaStreamTrack)
  track.source = Track.Source.Microphone
  const publication = { source: Track.Source.Microphone, track, isMuted: false }
  const rooms: Room[] = []
  const originalOn = Room.prototype.on
  const observe = spyOn(Room.prototype, 'on').mockImplementation(function (this: Room, event, listener) {
    this.connect = async () => {
      rooms.push(this)
    }
    this.disconnect = async () => {}
    return originalOn.call(this, event, listener)
  })
  const microphone = spyOn(LocalParticipant.prototype, 'setMicrophoneEnabled').mockResolvedValue(undefined)
  const getPublication = spyOn(LocalParticipant.prototype, 'getTrackPublication').mockImplementation(source =>
    source === Track.Source.Microphone ? (publication as never) : undefined,
  )
  restoreRoomMocks.push(
    () => observe.mockRestore(),
    () => microphone.mockRestore(),
    () => getPublication.mockRestore(),
  )
  api.realtimeVoiceToken = async () => ({ url: 'wss://never-joined.invalid', token: 'synthetic-token' })
  await mountVoice()
  await act(async () => {
    button('Start call').click()
  })
  expect(container.textContent).toContain('Microphone: enabled')
  expect([...container.querySelectorAll('button')].some(item => item.textContent === 'Recover microphone')).toBe(false)
  await act(async () => {
    native.readyState = 'ended'
    native.dispatchEvent(new window.Event('ended'))
  })
  expect(container.textContent).toContain('Microphone: disabled')
  expect(container.textContent).not.toContain('Microphone: enabled')
  expect([...container.querySelectorAll('button')].some(item => item.textContent === 'Recover microphone')).toBe(false)
  await act(async () => {
    native.readyState = 'live'
    track.emit(TrackEvent.Restarted, track)
    rooms[0].emit(RoomEvent.Reconnecting)
    rooms[0].emit(RoomEvent.Reconnected)
  })
  expect(container.textContent).toContain('Microphone: enabled')
  expect([...container.querySelectorAll('button')].some(item => item.textContent === 'Recover microphone')).toBe(false)
  await act(async () => {
    await track.mute()
  })
  expect(container.textContent).toContain('Microphone: disabled')
  await act(async () => {
    await track.unmute()
  })
  expect(container.textContent).toContain('Microphone: enabled')
  await act(async () => {
    rooms[0].emit(RoomEvent.LocalTrackUnpublished, publication as never, rooms[0].localParticipant)
  })
  expect(container.textContent).toContain('Microphone: disabled')
  expect(microphone).toHaveBeenCalledTimes(1)
  expect(rooms).toHaveLength(1)
})

test('permission failure keeps current call and explicit recovery uses same participant without a token or telemetry', async () => {
  const current = await connectCall(Object.assign(new Error('PRIVATE DEVICE DETAILS'), { name: 'NotAllowedError' }))
  expect(container.textContent).toContain('Microphone permission was denied')
  expect(container.textContent).not.toContain('PRIVATE DEVICE DETAILS')
  expect(container.textContent).toContain('Connected')
  expect(container.textContent).toContain('Browser capture: absent')
  let tokens = 0
  api.realtimeVoiceToken = async () => {
    tokens++
    throw new Error('Recovery must not request another token')
  }
  const reportCount = reports.length
  const publicationListeners = current.listenerCount(RoomEvent.LocalTrackPublished)
  await act(async () => {
    button('Recover microphone').click()
  })
  expect(tokens).toBe(0)
  expect(container.textContent).not.toContain('Microphone permission was denied')
  expect(container.textContent).toContain('Browser capture: absent')
  expect(container.textContent).not.toContain('Microphone: enabled')
  expect(current.listenerCount(RoomEvent.LocalTrackPublished)).toBe(publicationListeners)
  expect(reports).toHaveLength(reportCount)
  expect([...container.querySelectorAll('button')].some(item => item.textContent === 'Recover microphone')).toBe(false)
})

test('successful explicit Unmute clears a prior capture failure only after live publication returns', async () => {
  const current = await connectCall(Object.assign(new Error('Private denial'), { name: 'NotAllowedError' }))
  expect(container.textContent).toContain('Microphone permission was denied')
  await act(async () => button('Mute').click())
  expect(container.textContent).toContain('Microphone permission was denied')
  expect(button('Recover microphone').disabled).toBe(true)
  const previousStream = globalThis.MediaStream
  class SyntheticStream {
    constructor(private tracks: MediaStreamTrack[]) {}
    getTracks() {
      return this.tracks
    }
    getAudioTracks() {
      return this.tracks
    }
  }
  Object.assign(globalThis, { MediaStream: SyntheticStream })
  restoreRoomMocks.push(() => {
    globalThis.MediaStream = previousStream
  })
  const native = Object.assign(new window.EventTarget(), {
    id: 'synthetic-unmute',
    kind: 'audio',
    enabled: true,
    muted: false,
    readyState: 'live',
    getConstraints: () => ({}),
    getSettings: () => ({}),
    stop() {},
  })
  const track = new LocalAudioTrack(native as unknown as MediaStreamTrack)
  track.source = Track.Source.Microphone
  const publication = { source: Track.Source.Microphone, track, isMuted: false } as LocalTrackPublication
  current.localParticipant.setMicrophoneEnabled = async () => publication
  current.localParticipant.getTrackPublication = () => publication
  await act(async () => button('Unmute').click())
  expect(container.textContent).toContain('Microphone: enabled')
  expect(container.textContent).not.toContain('Microphone permission was denied')
  expect([...container.querySelectorAll('button')].some(item => item.textContent === 'Recover microphone')).toBe(false)
})

test('pending microphone recovery disables mute controls so intentional mute cannot race restart', async () => {
  const current = await connectCall(Object.assign(new Error('Capture unavailable'), { name: 'NotReadableError' }))
  const pending = Promise.withResolvers<undefined>()
  current.localParticipant.setMicrophoneEnabled = () => pending.promise
  await act(async () => {
    button('Recover microphone').click()
  })
  expect(button('Mute').disabled).toBe(true)
  expect(button('Recovering microphone…').disabled).toBe(true)
  await act(async () => {
    button('Mute').click()
  })
  expect(button('Mute').getAttribute('aria-pressed')).toBe('false')
  await act(async () => {
    pending.resolve()
    await pending.promise
  })
  expect(button('Mute').disabled).toBe(false)
  expect(button('Mute').getAttribute('aria-pressed')).toBe('false')
})

test('pending intentional mute blocks duplicate microphone actions through reconnect without recapture', async () => {
  const current = await connectCall()
  const pending = Promise.withResolvers<undefined>()
  const enables: boolean[] = []
  current.localParticipant.setMicrophoneEnabled = enabled => {
    enables.push(enabled)
    return pending.promise
  }
  await act(async () => {
    button('Mute').click()
    button('Mute').click()
  })
  expect(enables).toEqual([false])
  expect(button('Mute').disabled).toBe(true)
  await act(async () => {
    current.emit(RoomEvent.Reconnecting)
    current.emit(RoomEvent.Reconnected)
  })
  expect(enables).toEqual([false])
  await act(async () => {
    pending.resolve()
    await pending.promise
  })
  expect(button('Unmute').getAttribute('aria-pressed')).toBe('true')
  expect(button('Unmute').disabled).toBe(false)
  expect(enables).toEqual([false])
})

test.each(['end', 'disconnect'] as const)(
  'late Unmute capture stops after %s without reviving the call',
  async exit => {
    const current = await connectCall()
    await act(async () => {
      button('Mute').click()
    })
    const pending = Promise.withResolvers<LocalTrackPublication | undefined>()
    const enables: boolean[] = []
    let stopped = 0
    let tokens = 0
    api.realtimeVoiceToken = async () => {
      tokens++
      throw new Error('Unmute must not request another token')
    }
    current.localParticipant.setMicrophoneEnabled = enabled => {
      enables.push(enabled)
      return pending.promise
    }
    await act(async () => {
      button('Unmute').click()
    })
    expect(button('Unmute').disabled).toBe(true)
    await act(async () => {
      if (exit === 'end') {
        api.endRealtimeVoice = async () => ({ ...session, status: 'ended' })
        button('End call').click()
      } else current.emit(RoomEvent.Disconnected)
    })
    expect(container.textContent).toContain(exit === 'end' ? 'Call ended' : 'Disconnected')
    await act(async () => {
      pending.resolve({
        source: Track.Source.Microphone,
        track: {
          stop() {
            stopped++
          },
        },
      } as unknown as LocalTrackPublication)
      await pending.promise
    })
    expect(stopped).toBe(1)
    expect(enables).toEqual([true])
    expect(tokens).toBe(0)
    expect(container.textContent).toContain(exit === 'end' ? 'Call ended' : 'Disconnected')
    expect(container.textContent).not.toContain('Microphone: enabled')
  },
)
