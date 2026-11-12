import { afterEach, beforeEach, expect, spyOn, test } from 'bun:test'
import { act, createElement } from 'react'
import type { createRoot as CreateRoot, Root } from 'react-dom/client'
import { renderToStaticMarkup } from 'react-dom/server'
import { Window } from 'happy-dom'
import { LocalParticipant, Room, RoomEvent, Track } from 'livekit-client'
import { api, type RealtimeVoiceSession } from '../api'
import { RealtimeVoice } from './RealtimeVoice'
import { VoiceSessionCard } from './VoiceSessionCard'

const original = {
  config: api.realtimeVoiceConfig,
  sessions: api.realtimeVoiceSessions,
  start: api.startRealtimeVoice,
  session: api.realtimeVoiceSession,
  token: api.realtimeVoiceToken,
  end: api.endRealtimeVoice,
  standby: api.standbyRealtimeVoice,
  resume: api.resumeRealtimeVoice,
}
const session: RealtimeVoiceSession = {
  id: 'voice-1',
  chat_id: 'chat-1',
  status: 'active',
  events: [{ event_id: 'event-1', kind: 'user', text: 'Hello', delivery: 'unknown', created_at: '' }],
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

beforeEach(async () => {
  window = new Window({ url: 'http://localhost' })
  Object.assign(globalThis, {
    window,
    document: window.document,
    navigator: window.navigator,
    sessionStorage: window.sessionStorage,
    HTMLElement: window.HTMLElement,
    HTMLDialogElement: window.HTMLDialogElement,
    Event: window.Event,
    MouseEvent: window.MouseEvent,
    IS_REACT_ACT_ENVIRONMENT: true,
  })
  window.HTMLDialogElement.prototype.showModal = function () {
    this.open = true
  }
  window.HTMLDialogElement.prototype.close = function () {
    this.open = false
  }
  // React DOM captures the document at initialization, after Happy DOM is installed.
  createRoot = (await import('react-dom/client')).createRoot
  container = window.document.createElement('div')
  window.document.body.append(container)
  root = createRoot(container)
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
  restoreOutputs()
  restoreOutputs = () => {}
  restoreRoomMocks.splice(0).forEach(restore => restore())
  window.happyDOM.close()
})

test('voice room is hidden when disabled', async () => {
  api.realtimeVoiceConfig = async () => ({ enabled: false })
  await act(async () => {
    root.render(createElement(RealtimeVoice, { sessionId: 'chat-1', disabled: false }))
  })
  expect(container.querySelector('button')).toBeNull()
})

test('reopens a saved room, shows durable transcript and ends it', async () => {
  api.realtimeVoiceConfig = async () => ({ enabled: true })
  let starts = 0
  api.startRealtimeVoice = async () => {
    starts++
    return session
  }
  api.realtimeVoiceSession = async id => {
    expect(id).toBe('voice-1')
    return session
  }
  api.realtimeVoiceToken = async () => {
    throw new Error('Token unavailable')
  }
  api.endRealtimeVoice = async () => ({ ...session, status: 'ended' })
  window.sessionStorage.setItem('ergonaut.realtimeVoice.chat-1', 'voice-1')
  await act(async () => {
    root.render(createElement(RealtimeVoice, { sessionId: 'chat-1', disabled: false }))
  })
  await act(async () => {
    button('Voice room').click()
  })
  expect(starts).toBe(0)
  expect(container.textContent).toContain('Hello')
  expect(container.textContent).toContain('Token unavailable')
  await act(async () => {
    button('End room').click()
  })
  expect(window.sessionStorage.getItem('ergonaut.realtimeVoice.chat-1')).toBeNull()
})

test('reconnects to a durable active session without browser storage', async () => {
  api.realtimeVoiceConfig = async () => ({ enabled: true })
  let starts = 0
  api.startRealtimeVoice = async () => {
    starts++
    return session
  }
  api.realtimeVoiceSession = async () => session
  api.realtimeVoiceSessions = async () => [session]
  api.realtimeVoiceToken = async () => {
    throw new Error('Token unavailable')
  }
  await act(async () => {
    root.render(createElement(RealtimeVoice, { sessionId: 'chat-1', disabled: false }))
  })
  await act(async () => {
    button('Voice room').click()
  })
  expect(starts).toBe(0)
  expect(container.textContent).toContain('Hello')
})

test('standby controls are absent when the flag is disabled', async () => {
  api.realtimeVoiceConfig = async () => ({ enabled: true, standby_enabled: false })
  api.realtimeVoiceSessions = async () => [{ ...session, standby_state: 'standby' }]
  let tokens = 0
  api.realtimeVoiceToken = async () => {
    tokens++
    throw new Error('Token unavailable')
  }
  await act(async () => {
    root.render(createElement(RealtimeVoice, { sessionId: 'chat-1', disabled: false }))
  })
  await act(async () => {
    button('Voice room').click()
  })
  expect(tokens).toBe(1)
  expect(container.textContent).not.toContain('Resume manually')
  expect(container.textContent).not.toContain('Standby while waiting')
  expect(container.textContent).toContain('Reconnect')
})

test('opening a standby room never resumes or requests a token', async () => {
  api.realtimeVoiceConfig = async () => ({ enabled: true, standby_enabled: true })
  const standby = { ...session, standby_state: 'standby' as const }
  api.realtimeVoiceSessions = async () => [standby]
  let resumes = 0
  let tokens = 0
  api.resumeRealtimeVoice = async () => {
    resumes++
    return { ...standby, standby_state: 'resuming' }
  }
  api.realtimeVoiceToken = async () => {
    tokens++
    throw new Error('Token unavailable')
  }
  await act(async () => {
    root.render(createElement(RealtimeVoice, { sessionId: 'chat-1', disabled: false }))
  })
  await act(async () => {
    button('Voice room').click()
  })
  expect(resumes).toBe(0)
  expect(tokens).toBe(0)
  expect(container.textContent).toContain('Microphone: unknown')
  expect(container.textContent).toContain('Provider: closed')
  expect(container.textContent).toContain('Resume manually')
  await act(async () => {
    button('Resume').click()
  })
  expect(resumes).toBe(1)
  expect(tokens).toBe(1)
  expect(container.textContent).toContain('Token unavailable')
  expect(container.textContent).toContain('Provider: unknown')
})

test('a pending standby stays pending on opening without connecting or resuming', async () => {
  api.realtimeVoiceConfig = async () => ({ enabled: true, standby_enabled: true })
  api.realtimeVoiceSessions = async () => [{ ...session, standby_state: 'requested' }]
  let tokens = 0
  api.realtimeVoiceToken = async () => {
    tokens++
    throw new Error('unexpected')
  }
  await act(async () => {
    root.render(createElement(RealtimeVoice, { sessionId: 'chat-1', disabled: false }))
  })
  await act(async () => {
    button('Voice room').click()
  })
  expect(tokens).toBe(0)
  expect(container.textContent).toContain('waiting for the bridge')
  expect(container.textContent).not.toContain('Resume manually')
})

test('disconnecting warns about microphone input without offering resume', async () => {
  api.realtimeVoiceConfig = async () => ({ enabled: true, standby_enabled: true })
  api.realtimeVoiceSessions = async () => [{ ...session, standby_state: 'closing' }]
  await act(async () => {
    root.render(createElement(RealtimeVoice, { sessionId: 'chat-1', disabled: false }))
  })
  await act(async () => {
    button('Voice room').click()
  })
  expect(container.textContent).toContain('Disconnecting')
  expect(container.textContent).toContain('microphone input is becoming unavailable')
  expect(container.textContent).toContain('speech may be cut off')
  expect(container.textContent).not.toContain('Resume manually')
})

test('standby retains the boundary uncertainty and remains manually resumable', async () => {
  api.realtimeVoiceConfig = async () => ({ enabled: true, standby_enabled: true })
  api.realtimeVoiceSessions = async () => [
    { ...session, standby_state: 'standby', state: { standby_audio_uncertain: true } },
  ]
  await act(async () => {
    root.render(createElement(RealtimeVoice, { sessionId: 'chat-1', disabled: false }))
  })
  await act(async () => {
    button('Voice room').click()
  })
  expect(container.textContent).toContain('Lost speech is unknown')
  expect(button('Resume')).toBeTruthy()
})

test('an ended pending standby reports the unconfirmed boundary', async () => {
  api.realtimeVoiceConfig = async () => ({ enabled: true, standby_enabled: true })
  api.realtimeVoiceSessions = async () => [{ ...session, standby_state: 'requested' }]
  api.endRealtimeVoice = async () => ({ ...session, status: 'ended', standby_state: 'requested' })
  await act(async () => {
    root.render(createElement(RealtimeVoice, { sessionId: 'chat-1', disabled: false }))
  })
  await act(async () => {
    button('Voice room').click()
  })
  await act(async () => {
    button('End room').click()
  })
  expect(container.textContent).toContain('ended before standby completed')
  expect(container.textContent).toContain('may have been interrupted')
})

test('resume is single flight and a refused resume remains in standby', async () => {
  api.realtimeVoiceConfig = async () => ({ enabled: true, standby_enabled: true })
  api.realtimeVoiceSessions = async () => [{ ...session, standby_state: 'standby' }]
  const { promise, reject } = Promise.withResolvers<RealtimeVoiceSession>()
  let calls = 0
  api.resumeRealtimeVoice = () => {
    calls++
    return promise
  }
  await act(async () => {
    root.render(createElement(RealtimeVoice, { sessionId: 'chat-1', disabled: false }))
  })
  await act(async () => {
    button('Voice room').click()
  })
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
  await act(async () => {
    root.render(createElement(RealtimeVoice, { sessionId: 'chat-1', disabled: false }))
  })
  await act(async () => {
    button('Voice room').click()
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

test('device loss updates the selector, close tears down listeners, and reopening starts at default', async () => {
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
    ;(container.querySelector('[aria-label="Close voice room"]') as HTMLButtonElement).click()
  })
  expect(routing.listeners.size).toBe(0)
  await act(async () => {
    button('Voice room').click()
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
  await act(async () => {
    root.render(createElement(RealtimeVoice, { sessionId: 'chat-1', disabled: false }))
  })
  await act(async () => {
    button('Voice room').click()
  })
  const dialog = container.querySelector('dialog')!
  expect(dialog.querySelector('[role="status"]')?.textContent).toBe(restriction)
  expect(dialog.textContent!.indexOf(restriction)).toBeLessThan(dialog.textContent!.indexOf('Hello'))
})

test('mocked room events reapply outputs through reconnect and new-room standby resume without capture', async () => {
  const routing = installOutputs()
  const rooms: Room[] = []
  const originalOn = Room.prototype.on
  // connect/disconnect are instance fields in LiveKit 2.23.0. Replace them
  // when the component registers its first handler, before it can connect.
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
  restoreRoomMocks.push(
    () => observe.mockRestore(),
    () => microphone.mockRestore(),
  )
  api.realtimeVoiceConfig = async () => ({ enabled: true, standby_enabled: true })
  const live = { ...session, standby_state: 'live' as const }
  api.realtimeVoiceSessions = async () => [live]
  api.realtimeVoiceSession = async () => live
  api.realtimeVoiceToken = async () => ({ url: 'wss://never-joined.invalid', token: 'not-a-real-token' })
  api.standbyRealtimeVoice = async () => ({ ...live, standby_state: 'standby' })
  api.resumeRealtimeVoice = async () => ({ ...live, standby_state: 'resuming' })
  await act(async () => {
    root.render(createElement(RealtimeVoice, { sessionId: 'chat-1', disabled: false }))
  })
  await act(async () => {
    button('Voice room').click()
  })
  expect(rooms).toHaveLength(1)
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
