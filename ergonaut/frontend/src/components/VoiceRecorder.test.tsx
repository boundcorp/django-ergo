import { afterEach, beforeEach, describe, expect, test } from 'bun:test'
import { act } from 'react'
import type { Root } from 'react-dom/client'
import { Window } from 'happy-dom'
import { api } from '../api'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { NewThread } from '../pages/NewThread'
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
  api.voiceConfig = async () => ({ enabled: true, max_duration: 60 })
  api.transcribeVoice = async () => ({ text: 'spoken words' })
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
})

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
    const input = container.querySelector('textarea') as HTMLTextAreaElement
    input.setSelectionRange(7, 14)
    await click('Record voice message')
    await click('Stop recording')
    expect(session).toBe('main')
    expect(input.value).toBe('before spoken after')
    expect(win.localStorage.getItem('ergonaut.draft.new-thread.example')).toBe('before spoken after')
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

  test('automatically stops at the configured duration', async () => {
    api.voiceConfig = async () => ({ enabled: true, max_duration: 0.01 })
    let result = ''
    await mount(text => {
      result = text
    })
    await click('Record voice message')
    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, 300))
    })
    expect(stopped).toBe(1)
    expect(result).toBe('spoken words')
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

  test('pagehide discards recording and stops microphone tracks', async () => {
    let uploads = 0
    api.transcribeVoice = async () => {
      uploads++
      return { text: 'unexpected' }
    }
    await mount()
    await click('Record voice message')
    await act(async () => win.dispatchEvent(new win.Event('pagehide')))
    expect(stopped).toBe(1)
    expect(uploads).toBe(0)
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
