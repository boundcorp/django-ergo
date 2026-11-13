import { afterEach, beforeEach, describe, expect, test } from 'bun:test'
import { act } from 'react'
import type { Root } from 'react-dom/client'
import { Window } from 'happy-dom'
import { api } from '../api'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { NewThread } from '../pages/NewThread'
import { Chat } from '../pages/Chat'
import { VoiceRecorder } from './VoiceRecorder'

let win: Window
let root: Root
let container: HTMLDivElement
let stopped: number
let requested: number
let instances: FakeRecorder[]
let supportedTypes: string[]
let actualType: string | undefined
const original = {
  config: api.voiceConfig,
  transcribe: api.transcribeVoice,
  openRoot: api.openRoot,
  models: api.models,
  newThread: api.newThread,
  send: api.send,
  session: api.session,
  attachments: api.attachments,
  pins: api.pins,
  realtimeVoiceConfig: api.realtimeVoiceConfig,
  realtimeVoiceSessions: api.realtimeVoiceSessions,
  sessionUsage: api.sessionUsage,
}

class FakeRecorder {
  state: 'recording' | 'inactive' = 'inactive'
  mimeType: string
  ondataavailable: ((event: { data: Blob }) => void) | null = null
  onstop: (() => void) | null = null
  onerror: (() => void) | null = null
  static isTypeSupported(type: string) {
    return supportedTypes.includes(type)
  }
  constructor(_stream: MediaStream, options: MediaRecorderOptions) {
    this.mimeType = actualType ?? options.mimeType ?? ''
    instances.push(this)
  }
  start() {
    this.state = 'recording'
  }
  stop() {
    this.state = 'inactive'
    this.ondataavailable?.({ data: new Blob(['audio'], { type: this.mimeType }) })
    this.onstop?.()
  }
}

function button(label: string) {
  const found = [...container.querySelectorAll('button')].find(
    el => el.getAttribute('aria-label')?.startsWith(label) || el.textContent === label,
  )
  if (!found) throw new Error(`Missing button ${label}`)
  return found
}
async function click(label: string) {
  await act(async () => {
    button(label).click()
    await Promise.resolve()
  })
}
async function mount(onText = (_text: string) => {}, sessionId = 'thread') {
  await act(async () => {
    root.render(<VoiceRecorder key={sessionId} sessionId={sessionId} disabled={false} onText={onText} />)
    await Promise.resolve()
  })
}

const session = {
  id: 'thread',
  bot: 'example',
  title: 'Thread',
  role: 'thread',
  parent_id: null,
  status: 'active',
  username: '',
  created_at: '2026-11-13T12:00:00Z',
  updated_at: '2026-11-13T12:00:00Z',
}
const queuedTurn = {
  session_id: 'thread',
  call_id: null,
  type: null,
  text: '',
  suggestions: [],
  approvals: [],
  error: '',
  queued: true,
}

async function mountConsumer(kind: 'chat' | 'new-thread', draft = 'existing ') {
  win.localStorage.setItem(`ergonaut.draft.${kind === 'chat' ? 'thread' : 'new-thread.example'}`, draft)
  api.openRoot = async () => ({ ...session, id: 'main', role: 'main' })
  api.models = async () => ({ models: [], default: '', default_engine_type: '' })
  api.session = async id => ({ session: { ...session, id }, messages: [], calls: [] })
  api.attachments = async () => []
  api.pins = async () => []
  api.newThread = async () => session
  const router = createMemoryRouter(
    [
      { path: '/bots/:name/new-thread', element: <NewThread onChange={() => {}} userId="user" /> },
      { path: '/s/:id', element: kind === 'chat' ? <Chat onChange={() => {}} /> : <p>Created</p> },
      { path: '/away', element: <p>Away</p> },
    ],
    { initialEntries: [kind === 'chat' ? '/s/thread' : '/bots/example/new-thread'] },
  )
  await act(async () => {
    root.render(<RouterProvider router={router} />)
    await Promise.resolve()
    await Promise.resolve()
  })
  const input = composerInput()
  input.setSelectionRange(input.value.length, input.value.length)
  return { router, input }
}

function composerInput() {
  // Chat also mounts an illustration textarea inside its closed dialog.
  const input = container.querySelector<HTMLTextAreaElement>(
    'textarea[aria-label="Message"], textarea[aria-label="First message"]',
  )
  if (!input) throw new Error('Missing message composer')
  return input
}

function editDraft(input: HTMLTextAreaElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(win.HTMLTextAreaElement.prototype, 'value')!.set!
  act(() => {
    setter.call(input, value)
    input.dispatchEvent(new win.Event('input', { bubbles: true }))
    input.dispatchEvent(new win.Event('change', { bubbles: true }))
  })
  input.setSelectionRange(value.length, value.length)
}

async function submitForm(input: HTMLTextAreaElement) {
  await act(async () => {
    input.closest('form')!.dispatchEvent(new win.Event('submit', { bubbles: true, cancelable: true }))
    await Promise.resolve()
  })
}

beforeEach(async () => {
  win = new Window({ url: 'https://localhost' })
  Object.defineProperty(win, 'isSecureContext', { configurable: true, value: true })
  stopped = 0
  requested = 0
  instances = []
  supportedTypes = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4']
  actualType = undefined
  Object.assign(globalThis, {
    window: win,
    document: win.document,
    navigator: win.navigator,
    HTMLElement: win.HTMLElement,
    Event: win.Event,
    localStorage: win.localStorage,
    requestAnimationFrame: (callback: FrameRequestCallback) => win.requestAnimationFrame(callback),
    MediaRecorder: FakeRecorder,
    IS_REACT_ACT_ENVIRONMENT: true,
    EventSource: class {
      close() {}
    },
    ResizeObserver: class {
      observe() {}
      disconnect() {}
    },
  })
  Object.defineProperty(win.navigator, 'mediaDevices', {
    configurable: true,
    value: {
      getUserMedia: async () => {
        requested++
        return { getTracks: () => [{ stop: () => stopped++ }] }
      },
    },
  })
  win.matchMedia = (() => ({
    matches: false,
    addEventListener() {},
    removeEventListener() {},
  })) as typeof win.matchMedia
  win.HTMLElement.prototype.scrollTo = function () {}
  api.send = async () => queuedTurn
  api.voiceConfig = async () => ({ enabled: true, max_duration: 60 })
  api.transcribeVoice = async () => ({ text: 'spoken words' })
  api.realtimeVoiceConfig = async () => ({ enabled: false })
  api.realtimeVoiceSessions = async () => []
  api.sessionUsage = async () => ({
    until: '2026-11-13T12:00:00Z',
    hours: [],
    thread_tokens: 0,
    agent_tokens: 0,
  })
  // React DOM must initialize after the test document exists.
  const { createRoot } = await import('react-dom/client')
  container = win.document.createElement('div')
  win.document.body.append(container)
  root = createRoot(container)
})
afterEach(() => {
  act(() => root.unmount())
  api.voiceConfig = original.config
  api.openRoot = original.openRoot
  api.models = original.models
  api.transcribeVoice = original.transcribe
  api.newThread = original.newThread
  api.send = original.send
  api.session = original.session
  api.attachments = original.attachments
  api.pins = original.pins
  api.realtimeVoiceConfig = original.realtimeVoiceConfig
  api.realtimeVoiceSessions = original.realtimeVoiceSessions
  api.sessionUsage = original.sessionUsage
})

for (const kind of ['chat', 'new-thread'] as const) {
  describe(`${kind} voice composer`, () => {
    test('Stop waits for final speech and only updates the editable draft', async () => {
      const pending = Promise.withResolvers<{ text: string }>()
      api.transcribeVoice = () => pending.promise
      const sent: string[] = []
      api.send = async (_id, text) => {
        sent.push(text)
        return queuedTurn
      }
      const { input } = await mountConsumer(kind)
      await click('Record voice message')
      await click('Stop recording')
      expect(stopped).toBe(1)
      expect(input.value).toBe('existing ')
      editDraft(input, 'edited while waiting ')
      await act(async () => {
        pending.resolve({ text: 'final speech' })
        await Promise.resolve()
      })
      expect(input.value).toBe('edited while waiting final speech')
      expect(sent).toEqual([])
      expect(input.disabled).toBe(false)
    })

    for (const action of ['stop', 'send'] as const) {
      test(`${action} preserves selected draft text changed while transcription is pending`, async () => {
        const pending = Promise.withResolvers<{ text: string }>()
        api.transcribeVoice = () => pending.promise
        const sent: string[] = []
        api.send = async (_id, text) => {
          sent.push(text)
          return queuedTurn
        }
        const { input } = await mountConsumer(kind)
        await click('Record voice message')
        if (action === 'stop') await click('Stop recording')
        else await submitForm(input)
        editDraft(input, 'before selected after')
        input.setSelectionRange(7, 15)
        await act(async () => {
          pending.resolve({ text: 'spoken ' })
          await Promise.resolve()
          await Promise.resolve()
        })
        if (action === 'stop') {
          expect(input.value).toBe('before spoken selected after')
          expect(sent).toEqual([])
        } else expect(sent).toEqual(['before spoken selected after'])
      })
    }

    test('Send stops recording, awaits final speech and serializes duplicate click, Enter and reentrant send', async () => {
      const pending = Promise.withResolvers<{ text: string }>()
      api.transcribeVoice = () => pending.promise
      const sent: string[] = []
      let created = 0
      const { input } = await mountConsumer(kind, '')
      api.newThread = async () => {
        created++
        return session
      }
      api.send = async (_id, text) => {
        sent.push(text)
        input.closest('form')!.dispatchEvent(new win.Event('submit', { bubbles: true, cancelable: true }))
        return queuedTurn
      }
      await click('Record voice message')
      expect(button('Send').disabled).toBe(false)
      await act(async () => {
        button('Send').click()
        button('Send').click()
        input.dispatchEvent(new win.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }))
        await Promise.resolve()
      })
      expect(stopped).toBe(1)
      expect(sent).toEqual([])
      editDraft(input, 'typed meanwhile ')
      await act(async () => {
        pending.resolve({ text: 'final speech' })
        await Promise.resolve()
        await Promise.resolve()
      })
      expect(sent).toEqual(['typed meanwhile final speech'])
      expect(created).toBe(kind === 'new-thread' ? 1 : 0)
    })

    test('Send during Stop processing uses the same finalization and latest draft', async () => {
      const pending = Promise.withResolvers<{ text: string }>()
      let uploads = 0
      api.transcribeVoice = () => {
        uploads++
        return pending.promise
      }
      const sent: string[] = []
      api.send = async (_id, text) => {
        sent.push(text)
        return queuedTurn
      }
      const { input } = await mountConsumer(kind)
      await click('Record voice message')
      const stop = button('Stop recording')
      await act(async () => {
        stop.click()
        stop.click()
        await Promise.resolve()
      })
      await submitForm(input)
      await submitForm(input)
      editDraft(input, 'latest editable draft ')
      expect(sent).toEqual([])
      await act(async () => {
        pending.resolve({ text: 'complete speech' })
        await Promise.resolve()
        await Promise.resolve()
      })
      expect(uploads).toBe(1)
      expect(sent).toEqual(['latest editable draft complete speech'])
    })

    test('failed Send keeps draft and audio; explicit retry restores a draft without accidental Send', async () => {
      let uploads = 0
      const blobs: Blob[] = []
      api.transcribeVoice = async (_id, blob) => {
        blobs.push(blob)
        if (++uploads === 1) throw new Error('Offline. Retry transcription or download the recording.')
        return { text: 'recovered speech' }
      }
      const sent: string[] = []
      api.send = async (_id, text) => {
        sent.push(text)
        return queuedTurn
      }
      const { input } = await mountConsumer(kind)
      await click('Record voice message')
      await submitForm(input)
      expect(input.value).toBe('existing ')
      expect(sent).toEqual([])
      expect(button('Download recording')).toBeTruthy()
      await submitForm(input)
      expect(sent).toEqual([])
      expect(uploads).toBe(1)
      editDraft(input, 'draft retained ')
      await click('Retry')
      expect(blobs[1]).toBe(blobs[0])
      expect(requested).toBe(1)
      expect(input.value).toBe('draft retained recovered speech')
      expect(sent).toEqual([])
      await submitForm(input)
      expect(sent).toEqual(['draft retained recovered speech'])
    })

    test('cancel invalidates an awaiting Send and permits a later explicit text send', async () => {
      const pending = Promise.withResolvers<{ text: string }>()
      api.transcribeVoice = () => pending.promise
      const sent: string[] = []
      api.send = async (_id, text) => {
        sent.push(text)
        return queuedTurn
      }
      const { input } = await mountConsumer(kind)
      await click('Record voice message')
      await submitForm(input)
      await click('Cancel')
      await act(async () => {
        pending.resolve({ text: 'discarded speech' })
        await Promise.resolve()
      })
      expect(input.value).toBe('existing ')
      expect(sent).toEqual([])
      await submitForm(input)
      expect(sent).toEqual(['existing '])
    })

    test('navigation invalidates finalization, even if the transport resolves after abort', async () => {
      const pending = Promise.withResolvers<{ text: string }>()
      let signal: AbortSignal | undefined
      api.transcribeVoice = (_id, _blob, controller) => {
        signal = controller
        return pending.promise
      }
      const sent: string[] = []
      api.send = async (_id, text) => {
        sent.push(text)
        return queuedTurn
      }
      const { router, input } = await mountConsumer(kind)
      await click('Record voice message')
      await submitForm(input)
      await act(async () => {
        await router.navigate('/away')
      })
      await act(async () => {
        pending.resolve({ text: 'late speech' })
        await Promise.resolve()
      })
      expect(signal?.aborted).toBe(true)
      expect(container.textContent).toBe('Away')
      expect(sent).toEqual([])
    })

    test('changing route parameters invalidates speech without contaminating the next draft', async () => {
      const pending = Promise.withResolvers<{ text: string }>()
      api.transcribeVoice = () => pending.promise
      const sent: string[] = []
      api.send = async (_id, text) => {
        sent.push(text)
        return queuedTurn
      }
      const { router, input } = await mountConsumer(kind)
      win.localStorage.setItem(`ergonaut.draft.${kind === 'chat' ? 'other' : 'new-thread.other'}`, 'other draft')
      await click('Record voice message')
      await submitForm(input)
      await act(async () => {
        await router.navigate(kind === 'chat' ? '/s/other' : '/bots/other/new-thread')
        await Promise.resolve()
      })
      await act(async () => {
        pending.resolve({ text: 'old route speech' })
        await Promise.resolve()
      })
      expect(composerInput().value).toBe('other draft')
      expect(sent).toEqual([])
    })

    test('ordinary text Send remains on the normal consumer handler', async () => {
      const sent: string[] = []
      api.send = async (_id, text) => {
        sent.push(text)
        return queuedTurn
      }
      const { input } = await mountConsumer(kind, 'text only')
      await submitForm(input)
      expect(sent).toEqual(['text only'])
      expect(requested).toBe(0)
    })
  })
}

describe('voice recorder', () => {
  test('new thread inserts dictated text at the selection and keeps it as a draft', async () => {
    win.localStorage.setItem('ergonaut.draft.new-thread.example', 'before replace after')
    api.openRoot = async () => ({
      id: 'main',
      bot: 'example',
      title: '',
      role: 'main',
      parent_id: null,
      status: '',
      username: '',
      created_at: '',
      updated_at: '',
    })
    api.models = async () => ({ models: [], default: '', default_engine_type: '' })
    let session = ''
    api.transcribeVoice = async id => {
      session = id
      return { text: 'spoken' }
    }
    const router = createMemoryRouter(
      [{ path: '/bots/:name/new-thread', element: <NewThread onChange={() => {}} userId="user" /> }],
      { initialEntries: ['/bots/example/new-thread'] },
    )
    await act(async () => {
      root.render(<RouterProvider router={router} />)
      await Promise.resolve()
      await Promise.resolve()
    })
    const input = composerInput()
    input.setSelectionRange(7, 14)
    await click('Record voice message')
    await click('Stop recording')
    expect(session).toBe('main')
    expect(input.value).toBe('before spokenreplace after')
    expect(win.localStorage.getItem('ergonaut.draft.new-thread.example')).toBe('before spokenreplace after')
    expect(router.state.location.pathname).toBe('/bots/example/new-thread')
  })

  test('requests permission only on click and inserts the transcription after stopping', async () => {
    let result = ''
    await mount(text => {
      result = text
    })
    expect(requested).toBe(0)
    expect(button('Record voice message').getAttribute('aria-label')).toContain('audio sent to OpenAI')
    expect(button('Record voice message').title).toContain('OpenAI')
    await click('Record voice message')
    expect(requested).toBe(1)
    expect(container.querySelector('[aria-live]')?.textContent).toContain('Recording')
    expect(button('Stop recording').title).toBe('Stop recording')
    expect(button('Cancel').title).toBe('Cancel')
    await click('Stop recording')
    expect(stopped).toBe(1)
    expect(result).toBe('spoken words')
  })

  test('prefers supported WebM over an Ogg browser default', async () => {
    let uploaded: Blob | undefined
    api.transcribeVoice = async (_id, blob) => {
      uploaded = blob
      return { text: 'ok' }
    }
    await mount()
    await click('Record voice message')
    expect(instances[0].mimeType).toBe('audio/webm;codecs=opus')
    await click('Stop recording')
    expect(uploaded?.type).toBe('audio/webm;codecs=opus')
  })

  test('falls back to supported MP4 when WebM is unavailable', async () => {
    supportedTypes = ['audio/mp4']
    let uploaded: Blob | undefined
    api.transcribeVoice = async (_id, blob) => {
      uploaded = blob
      return { text: 'ok' }
    }
    await mount()
    await click('Record voice message')
    await click('Stop recording')
    expect(uploaded?.type).toBe('audio/mp4')
  })

  test('disables recording when only Ogg is supported', async () => {
    supportedTypes = ['audio/ogg']
    await mount()
    expect(button('Record voice message').disabled).toBe(true)
    expect(button('Record voice message').title).toContain('compatible browser')
    expect(requested).toBe(0)
  })

  test('does not upload if the recorder returns an unsupported format', async () => {
    actualType = 'audio/ogg'
    let calls = 0
    api.transcribeVoice = async () => {
      calls++
      return { text: 'ok' }
    }
    await mount()
    await click('Record voice message')
    await click('Stop recording')
    expect(calls).toBe(0)
    expect(stopped).toBe(1)
    expect(container.textContent).toContain('unsupported audio format')
    expect(button('Download recording')).toBeTruthy()
    expect([...container.querySelectorAll('button')].some(el => el.getAttribute('aria-label') === 'Retry')).toBe(false)
    expect(win.document.activeElement).toBe(button('Dismiss'))
  })

  test('downloads an unsupported Ogg recording with the correct extension', async () => {
    actualType = 'audio/ogg'
    let filename = ''
    const originalClick = win.HTMLAnchorElement.prototype.click
    const originalCreate = URL.createObjectURL
    const originalRevoke = URL.revokeObjectURL
    win.HTMLAnchorElement.prototype.click = function () {
      filename = this.download
    }
    URL.createObjectURL = () => 'blob:recording'
    URL.revokeObjectURL = () => {}
    try {
      await mount()
      await click('Record voice message')
      await click('Stop recording')
      await click('Download recording')
      expect(filename).toBe('recording.ogg')
    } finally {
      win.HTMLAnchorElement.prototype.click = originalClick
      URL.createObjectURL = originalCreate
      URL.revokeObjectURL = originalRevoke
    }
  })

  test('offers a new recording rather than uploading empty audio', async () => {
    let calls = 0
    api.transcribeVoice = async () => {
      calls++
      return { text: 'ok' }
    }
    await mount()
    await click('Record voice message')
    await act(async () => {
      instances[0].state = 'inactive'
      instances[0].onstop?.()
    })
    expect(calls).toBe(0)
    expect(container.textContent).toContain('No audio was captured')
    await click('Retry')
    expect(requested).toBe(2)
  })

  test('escape discards recording and stops tracks without upload', async () => {
    let calls = 0
    api.transcribeVoice = async () => {
      calls++
      return { text: '' }
    }
    await mount()
    await click('Record voice message')
    await act(async () => {
      win.dispatchEvent(new win.KeyboardEvent('keydown', { key: 'Escape' }))
    })
    expect(stopped).toBe(1)
    expect(calls).toBe(0)
    expect(button('Record voice message')).toBeTruthy()
  })

  test('cancel while permission is pending disposes a subsequently granted stream', async () => {
    const { promise, resolve } = Promise.withResolvers<unknown>()
    Object.defineProperty(win.navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: () => promise } })
    await mount()
    await click('Record voice message')
    await click('Cancel')
    await act(async () => {
      resolve({ getTracks: () => [{ stop: () => stopped++ }] })
      await Promise.resolve()
    })
    expect(stopped).toBe(1)
    expect(instances).toHaveLength(0)
  })

  test('cancel aborts processing and ignores a late response', async () => {
    let signal: AbortSignal | undefined
    const { promise, resolve } = Promise.withResolvers<{ text: string }>()
    let result = ''
    api.transcribeVoice = async (_id, _blob, s) => {
      signal = s
      return promise
    }
    await mount(text => {
      result = text
    })
    await click('Record voice message')
    await click('Stop recording')
    expect(container.querySelector('[aria-live]')?.textContent).toContain('Transcribing audio with OpenAI')
    await click('Cancel')
    expect(signal?.aborted).toBe(true)
    await act(async () => {
      resolve({ text: 'late' })
      await Promise.resolve()
    })
    expect(result).toBe('')
  })

  test('retries failed upload without rerecording', async () => {
    let calls = 0
    api.transcribeVoice = async () => {
      calls++
      if (calls === 1) throw new Error('Rate limited')
      return { text: 'ok' }
    }
    let result = ''
    await mount(text => {
      result = text
    })
    await click('Record voice message')
    await click('Stop recording')
    expect(container.querySelector('[aria-live]')?.textContent).toContain('Rate limited')
    expect(container.querySelector('[role="img"]')?.getAttribute('title')).toBe('Rate limited')
    expect(button('Retry').title).toBe('Retry')
    expect(button('Download recording')).toBeTruthy()
    expect(button('Dismiss').title).toBe('Dismiss')
    await click('Retry')
    expect(result).toBe('ok')
    expect(requested).toBe(1)
  })

  test('dismisses microphone permission errors', async () => {
    Object.defineProperty(win.navigator, 'mediaDevices', {
      configurable: true,
      value: {
        getUserMedia: async () => {
          throw Object.assign(new Error('denied'), { name: 'NotAllowedError' })
        },
      },
    })
    await mount()
    await click('Record voice message')
    expect(container.textContent).toContain('Microphone access was denied')
    await click('Dismiss')
    expect(button('Record voice message')).toBeTruthy()
  })

  test('continues past the configured duration and uploads all captured chunks', async () => {
    api.voiceConfig = async () => ({ enabled: true, max_duration: 0.01 })
    let uploaded: Blob | undefined
    api.transcribeVoice = async (_id, blob) => {
      uploaded = blob
      return { text: 'ok' }
    }
    await mount()
    const originalNow = Date.now
    try {
      Date.now = () => 0
      await click('Record voice message')
      instances[0].ondataavailable?.({ data: new Blob(['first']) })
      Date.now = () => 31 * 60 * 1000
      const { promise, resolve } = Promise.withResolvers<void>()
      setTimeout(resolve, 300)
      await act(async () => {
        await promise
      })
      expect(container.textContent).toContain('31:00')
    } finally {
      Date.now = originalNow
    }
    expect(stopped).toBe(0)
    await click('Stop recording')
    expect(await uploaded?.text()).toBe('firstaudio')
  })

  test('recorder errors retain final data for retry and download', async () => {
    let uploaded: Blob | undefined
    api.transcribeVoice = async (_id, blob) => {
      uploaded = blob
      return { text: 'ok' }
    }
    await mount()
    await click('Record voice message')
    const active = instances[0]
    active.ondataavailable?.({ data: new Blob(['first']) })
    await act(async () => {
      active.state = 'inactive'
      active.onerror?.()
      active.ondataavailable?.({ data: new Blob(['final']) })
      active.onstop?.()
    })
    expect(button('Download recording')).toBeTruthy()
    expect(uploaded).toBeUndefined()
    await click('Retry')
    expect(await uploaded?.text()).toBe('firstfinal')
  })

  test('acquires, reacquires and releases a screen wake lock', async () => {
    let releases = 0
    let requests = 0
    let releaseListener: (() => void) | undefined
    Object.defineProperty(win.navigator, 'wakeLock', {
      configurable: true,
      value: {
        request: async () => {
          requests++
          return {
            addEventListener: (_type: string, listener: () => void) => {
              releaseListener = listener
            },
            release: async () => {
              releases++
            },
          }
        },
      },
    })
    Object.defineProperty(win.document, 'visibilityState', { configurable: true, value: 'visible' })
    await mount()
    await click('Record voice message')
    expect(requests).toBe(1)
    await act(async () => {
      releaseListener?.()
      win.document.dispatchEvent(new win.Event('visibilitychange'))
      await Promise.resolve()
    })
    expect(requests).toBe(2)
    await click('Stop recording')
    expect(releases).toBe(1)
  })

  test('retries a pending wake lock rejected after returning to the tab', async () => {
    let requests = 0
    const pending = Promise.withResolvers<never>()
    Object.defineProperty(win.navigator, 'wakeLock', {
      configurable: true,
      value: {
        request: () => {
          requests++
          return requests === 1
            ? pending.promise
            : Promise.resolve({ addEventListener: () => {}, release: async () => {} })
        },
      },
    })
    Object.defineProperty(win.document, 'visibilityState', { configurable: true, value: 'visible' })
    await mount()
    await click('Record voice message')
    await act(async () => {
      Object.defineProperty(win.document, 'visibilityState', { configurable: true, value: 'hidden' })
      win.document.dispatchEvent(new win.Event('visibilitychange'))
      Object.defineProperty(win.document, 'visibilityState', { configurable: true, value: 'visible' })
      win.document.dispatchEvent(new win.Event('visibilitychange'))
      pending.reject(new Error('hidden'))
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(requests).toBe(2)
  })

  test('reacquires when the previous wake lock releases after the tab becomes visible', async () => {
    let requests = 0
    let releaseListener: (() => void) | undefined
    Object.defineProperty(win.navigator, 'wakeLock', {
      configurable: true,
      value: {
        request: async () => {
          requests++
          return {
            addEventListener: (_type: string, listener: () => void) => {
              releaseListener = listener
            },
            release: async () => {},
          }
        },
      },
    })
    Object.defineProperty(win.document, 'visibilityState', { configurable: true, value: 'visible' })
    await mount()
    await click('Record voice message')
    await act(async () => {
      Object.defineProperty(win.document, 'visibilityState', { configurable: true, value: 'hidden' })
      win.document.dispatchEvent(new win.Event('visibilitychange'))
      Object.defineProperty(win.document, 'visibilityState', { configurable: true, value: 'visible' })
      win.document.dispatchEvent(new win.Event('visibilitychange'))
      releaseListener?.()
      await Promise.resolve()
    })
    expect(requests).toBe(2)
  })

  test('recording works when wake lock is rejected', async () => {
    Object.defineProperty(win.navigator, 'wakeLock', {
      configurable: true,
      value: {
        request: async () => {
          throw new Error('not available')
        },
      },
    })
    Object.defineProperty(win.document, 'visibilityState', { configurable: true, value: 'visible' })
    await mount()
    await click('Record voice message')
    await click('Stop recording')
    expect(stopped).toBe(1)
  })

  test('releases a wake lock granted after cancellation', async () => {
    let releases = 0
    const { promise, resolve } = Promise.withResolvers<{
      release: () => Promise<void>
      addEventListener: () => void
    }>()
    Object.defineProperty(win.navigator, 'wakeLock', { configurable: true, value: { request: () => promise } })
    Object.defineProperty(win.document, 'visibilityState', { configurable: true, value: 'visible' })
    await mount()
    await click('Record voice message')
    await click('Cancel')
    await act(async () => {
      resolve({
        release: async () => {
          releases++
        },
        addEventListener: () => {},
      })
      await promise
    })
    expect(releases).toBe(1)
  })

  test('hides controls if configuration cannot be loaded', async () => {
    api.voiceConfig = async () => {
      throw new Error('unavailable')
    }
    await mount()
    expect(container.querySelector('button')).toBeNull()
  })

  test('explains missing browser recording support', async () => {
    Object.defineProperty(win.navigator, 'mediaDevices', { configurable: true, value: undefined })
    await mount()
    expect(button('Record voice message').title).toContain('compatible browser')
    expect(requested).toBe(0)
  })

  test('voice API uses session credentials, CSRF and multipart audio', async () => {
    const originalFetch = globalThis.fetch
    const requests: { url: string; options: RequestInit }[] = []
    win.document.cookie = 'csrftoken=token123'
    globalThis.fetch = (async (url: string | URL | Request, options?: RequestInit) => {
      requests.push({ url: String(url), options: options ?? {} })
      return new Response(
        JSON.stringify(requests.length === 1 ? { enabled: true, max_duration: 60 } : { text: 'hello' }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        },
      )
    }) as typeof fetch
    try {
      await original.config('thread')
      await original.transcribe('thread', new Blob(['audio'], { type: 'audio/webm' }), new AbortController().signal)
      expect(requests[0].url).toBe('/api/sessions/thread/voice')
      expect(requests[0].options.credentials).toBe('same-origin')
      expect(requests[1].url).toBe('/api/sessions/thread/voice')
      expect(requests[1].options.credentials).toBe('same-origin')
      expect((requests[1].options.headers as Record<string, string>)['X-CSRFToken']).toBe('token123')
      expect((requests[1].options.body as FormData).get('audio')).toBeTruthy()
    } finally {
      globalThis.fetch = originalFetch
    }
  })

  test('pagehide does not discard captured audio', async () => {
    let uploaded: Blob | undefined
    api.transcribeVoice = async (_id, blob) => {
      uploaded = blob
      return { text: 'ok' }
    }
    await mount()
    await click('Record voice message')
    instances[0].ondataavailable?.({ data: new Blob(['before sleep']) })
    await act(async () => win.dispatchEvent(new win.Event('pagehide')))
    expect(stopped).toBe(0)
    await click('Stop recording')
    expect(await uploaded?.text()).toBe('before sleepaudio')
  })

  test('session changes stop active tracks and disabled configuration hides controls', async () => {
    await mount()
    await click('Record voice message')
    api.voiceConfig = async () => ({ enabled: false, max_duration: 60 })
    await mount(undefined, 'other')
    expect(stopped).toBe(1)
    expect(container.querySelector('button')).toBeNull()
  })
})
