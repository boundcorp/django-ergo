import { afterEach, beforeEach, describe, expect, test } from 'bun:test'
import { act } from 'react'
import type { Root } from 'react-dom/client'
import { Window } from 'happy-dom'
import { api } from '../api'
import { VoiceRecorder } from './VoiceRecorder'

let win: Window
let root: Root
let container: HTMLDivElement
let stopped: number
let requested: number
let instances: FakeRecorder[]
const original = { config: api.voiceConfig, transcribe: api.transcribeVoice }

class FakeRecorder {
  state: 'recording' | 'inactive' = 'inactive'
  mimeType = 'audio/webm'
  ondataavailable: ((event: { data: Blob }) => void) | null = null
  onstop: (() => void) | null = null
  onerror: (() => void) | null = null
  constructor(_stream: MediaStream) {
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
    el => el.getAttribute('aria-label') === label || el.textContent === label,
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
  Object.assign(globalThis, {
    window: win,
    document: win.document,
    navigator: win.navigator,
    HTMLElement: win.HTMLElement,
    Event: win.Event,
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
  api.transcribeVoice = original.transcribe
})

describe('voice recorder', () => {
  test('requests permission only on click and inserts the transcription after stopping', async () => {
    let result = ''
    await mount(text => {
      result = text
    })
    expect(requested).toBe(0)
    await click('Record voice message')
    expect(requested).toBe(1)
    expect(container.textContent).toContain('Recording')
    await click('Stop recording')
    expect(stopped).toBe(1)
    expect(result).toBe('spoken words')
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
    expect(container.textContent).toContain('Audio is sent to the server and provider')
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
    expect(container.textContent).toContain('Rate limited')
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
    expect(container.textContent).toContain('supported browser')
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
