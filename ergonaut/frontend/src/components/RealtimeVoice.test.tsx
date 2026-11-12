import { afterEach, beforeEach, expect, test } from 'bun:test'
import { act, createElement } from 'react'
import type { createRoot as CreateRoot, Root } from 'react-dom/client'
import { renderToStaticMarkup } from 'react-dom/server'
import { Window } from 'happy-dom'
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
