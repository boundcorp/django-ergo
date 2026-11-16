import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { ReactNode, RefObject } from 'react'
import { createPortal } from 'react-dom'
import { Link, useLocation } from 'react-router-dom'
import { api } from '../api'
import { writeDraft } from '../draft'

type Config = { enabled: boolean; max_duration: number }
type Phase = 'idle' | 'permission' | 'recording' | 'processing' | 'error'

const recordingTypes = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/mpeg', 'audio/wav']
const acceptedTypes = [
  'audio/webm',
  'video/webm',
  'audio/mp4',
  'video/mp4',
  'audio/m4a',
  'audio/x-m4a',
  'audio/mpeg',
  'audio/mp3',
  'audio/wav',
  'audio/wave',
  'audio/x-wav',
]

function recordingType() {
  if (typeof MediaRecorder === 'undefined' || typeof MediaRecorder.isTypeSupported !== 'function') return undefined
  return recordingTypes.find(type => MediaRecorder.isTypeSupported(type))
}

function acceptedType(type: string) {
  return acceptedTypes.includes(type.split(';', 1)[0].trim().toLowerCase())
}

type VoiceFinalizer = { finalize: () => Promise<boolean> }
type SetText = (text: string | ((current: string) => string)) => void
type Send = (text: string, mode: 'send' | 'interrupt') => void | Promise<void>
type Entry = {
  key: string
  accountId: string | null
  origin: string
  text: string
  selection: number
  mounted: boolean
  saveText: SetText
  onSend: Send
  composer: RefObject<HTMLTextAreaElement | null>
  finalizer: RefObject<VoiceFinalizer | null>
  container: HTMLDivElement
  sessionId: string
  disabled: boolean
  active: boolean
  pending: { mode: 'send' | 'interrupt'; send: Send; delivering: boolean; sending?: boolean; message?: string } | null
  error: string
  uncertainThread: string
  listeners: Set<() => void>
  prune: () => void
  refresh: () => void
}
type Owner = {
  entries: Map<string, Entry>
  scoped: Map<string, { value: unknown; listeners: Set<() => void> }>
  accountId: string | null
  accountListeners: Set<() => void>
  refresh: () => void
  dock: RefObject<HTMLDivElement | null>
}
const VoiceOwner = createContext<Owner | null>(null)

function notify(entry: Entry) {
  entry.listeners.forEach(listener => listener())
  entry.prune()
  entry.refresh()
}

function updateText(entry: Entry, value: string | ((current: string) => string)) {
  entry.text = typeof value === 'function' ? value(entry.text) : value
  writeDraft(entry.key, entry.text)
  if (entry.mounted) entry.saveText(entry.text)
  notify(entry)
}

async function deliver(entry: Entry) {
  const pending = entry.pending
  if (!pending || pending.delivering) return
  pending.delivering = true
  const message = pending.message ?? entry.text
  pending.message = message
  if (entry.accountId) {
    notify(entry)
    try {
      const account = await api.me()
      if (account.id !== entry.accountId) throw new Error('The signed-in account changed.')
    } catch (error) {
      if (entry.pending !== pending) return
      const reason = error instanceof Error ? error.message : String(error)
      entry.error = `${reason} The message was not sent. Restore the original account and retry, or cancel the send.`
      pending.delivering = false
      notify(entry)
      return
    }
  }
  if (entry.pending !== pending) return
  pending.sending = true
  notify(entry)
  try {
    await pending.send(message, pending.mode)
    entry.pending = null
  } catch (error) {
    entry.error = error instanceof Error ? error.message : String(error)
    entry.uncertainThread =
      error && typeof error === 'object' && 'threadId' in error && typeof error.threadId === 'string'
        ? error.threadId
        : ''
    entry.pending = null
  }
  notify(entry)
}

/** Keeps microphone, captured audio and explicit sends alive across route changes. */
export function ComposerVoiceProvider({ children }: { children: ReactNode }) {
  const [, render] = useState(0)
  const dock = useRef<HTMLDivElement>(null)
  const owner = useRef<Owner | null>(null)
  if (!owner.current) {
    const accountListeners = new Set<() => void>()
    owner.current = {
      entries: new Map(),
      scoped: new Map(),
      accountId: null,
      accountListeners,
      dock,
      refresh: () => {
        render(n => n + 1)
        accountListeners.forEach(listener => listener())
      },
    }
  }
  const store = owner.current
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (![...store.entries.values()].some(entry => entry.active || entry.pending)) return
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [store])
  return (
    <VoiceOwner.Provider value={store}>
      {children}
      <div ref={dock} className="fixed bottom-4 right-4 z-50 rounded-card bg-surface shadow-lg" />
      {[...store.entries.values()].map(entry =>
        createPortal(
          <div className={!entry.mounted && !entry.active && !entry.pending && !entry.error ? 'hidden' : ''}>
            {!entry.mounted && (entry.active || entry.pending || entry.error) && (
              <div className="p-2 text-sm" role="status">
                Voice message for {entry.key}
                {entry.error && <p role="alert">{entry.error}</p>}
                {entry.uncertainThread && <Link to={`/s/${entry.uncertainThread}`}>Check conversation</Link>}
                {!entry.uncertainThread && <Link to={entry.origin}>Return to composer</Link>}
              </div>
            )}
            {!entry.mounted && <PendingSendRecovery entry={entry} />}
            {entry.sessionId && (
              <VoiceRecorder
                sessionId={entry.sessionId}
                accountId={entry.accountId}
                disabled={entry.disabled}
                background={!entry.mounted}
                finalizer={entry.finalizer}
                onActive={active => {
                  entry.active = active
                  notify(entry)
                }}
                onCancel={() => {
                  if (entry.pending?.sending) return
                  entry.pending = null
                  entry.error = ''
                  notify(entry)
                }}
                onText={transcript => {
                  const input = entry.mounted ? entry.composer.current : null
                  const start = input?.selectionStart ?? entry.selection
                  updateText(entry, current => current.slice(0, start) + transcript + current.slice(start))
                  entry.selection = start + transcript.length
                  if (input)
                    requestAnimationFrame(() => {
                      if (entry.mounted && entry.composer.current === input) {
                        input.focus()
                        input.setSelectionRange(entry.selection, entry.selection)
                      }
                    })
                  if (entry.pending) void deliver(entry)
                }}
              />
            )}
          </div>,
          entry.container,
          entry.key,
        ),
      )}
    </VoiceOwner.Provider>
  )
}

/** Prevents account changes while microphone data or a send is still owned by this page. */
export function useComposerAccount(accountId: string | null | undefined) {
  const owner = useContext(VoiceOwner)
  if (!owner) throw new Error('ComposerVoiceProvider is required')
  const [, render] = useState(0)
  useLayoutEffect(() => {
    const listener = () => render(n => n + 1)
    owner.accountListeners.add(listener)
    return () => {
      owner.accountListeners.delete(listener)
    }
  }, [owner])
  if (accountId) owner.accountId = accountId
  const active = [...owner.entries.values()].some(entry => entry.active || entry.pending)
  const clear = useCallback(() => {
    if ([...owner.entries.values()].some(entry => entry.active || entry.pending)) return false
    owner.entries.clear()
    owner.scoped.clear()
    owner.accountId = null
    owner.refresh()
    return true
  }, [owner])
  return { active, clear }
}

/** Route-independent, per-composer state for unsent attachments. */
export function useComposerAttachments<T>(key: string): [T[], (value: T[] | ((current: T[]) => T[])) => void] {
  const owner = useContext(VoiceOwner)
  if (!owner) throw new Error('ComposerVoiceProvider is required')
  let state = owner.scoped.get(key)
  if (!state) {
    state = { value: [], listeners: new Set() }
    owner.scoped.set(key, state)
  }
  const current = state
  const [, render] = useState(0)
  useLayoutEffect(() => {
    owner.scoped.set(key, current)
    const listener = () => render(n => n + 1)
    current.listeners.add(listener)
    return () => {
      current.listeners.delete(listener)
      if (
        !current.listeners.size &&
        Array.isArray(current.value) &&
        !current.value.length &&
        owner.scoped.get(key) === current
      )
        owner.scoped.delete(key)
    }
  }, [current, key, owner])
  function setValue(value: T[] | ((current: T[]) => T[])) {
    const previous = current.value as T[]
    current.value = typeof value === 'function' ? value(previous) : value
    current.listeners.forEach(listener => listener())
  }
  return [current.value as T[], setValue]
}

/** One submission gate for typed text and the recorder's final transcription. */
export function useComposerVoice({
  draftKey,
  text,
  setText: saveText,
  composer,
  onSend,
  canSubmit = true,
}: {
  draftKey: string
  text: string
  setText: SetText
  composer: RefObject<HTMLTextAreaElement | null>
  onSend: Send
  canSubmit?: boolean
}) {
  const owner = useContext(VoiceOwner)
  if (!owner) throw new Error('ComposerVoiceProvider is required')
  const location = useLocation()
  let entry = owner.entries.get(draftKey)
  if (!entry) {
    entry = {
      key: draftKey,
      origin: location.pathname + location.search,
      text,
      selection: text.length,
      mounted: false,
      saveText,
      onSend,
      composer,
      accountId: owner.accountId,
      finalizer: { current: null },
      container: document.createElement('div'),
      sessionId: '',
      disabled: false,
      active: false,
      pending: null,
      error: '',
      uncertainThread: '',
      listeners: new Set(),
      refresh: owner.refresh,
      prune: () => {
        const item = owner.entries.get(draftKey)
        if (item && !item.mounted && !item.active && !item.pending && !item.error) {
          owner.entries.delete(draftKey)
          item.container.remove()
        }
      },
    }
    owner.entries.set(draftKey, entry)
  }
  const current = entry
  current.saveText = saveText
  current.onSend = onSend
  current.composer = composer
  const [, render] = useState(0)
  useLayoutEffect(() => {
    owner.entries.set(draftKey, current)
    current.mounted = true
    current.saveText(current.text)
    const listener = () => render(n => n + 1)
    current.listeners.add(listener)
    owner.refresh()
    return () => {
      current.selection = current.composer.current?.selectionStart ?? current.selection
      current.mounted = false
      current.listeners.delete(listener)
      current.prune()
      if (owner.entries.get(draftKey) === current) owner.dock.current?.appendChild(current.container)
      owner.refresh()
    }
  }, [current, draftKey, owner])

  function setText(value: string | ((current: string) => string)) {
    updateText(current, value)
  }
  async function submit(mode: 'send' | 'interrupt' = 'send') {
    if (current.pending) return
    if (!canSubmit) {
      current.error = 'Wait for the current request or attachment upload to finish before sending.'
      notify(current)
      return
    }
    current.error = ''
    current.uncertainThread = ''
    current.selection = composer.current?.selectionStart ?? current.selection
    current.pending = { mode, send: current.onSend, delivering: false }
    notify(current)
    const ready = await (current.finalizer.current?.finalize() ?? Promise.resolve(true))
    if (ready) await deliver(current)
    // A failed transcription keeps the explicit send intent until retry or dismissal.
  }
  return {
    setText,
    submit,
    voiceActive: current.active,
    voice: { entry: current },
  }
}

export function ComposerVoice({ sessionId, disabled, entry }: { sessionId: string; disabled: boolean; entry: Entry }) {
  const owner = useContext(VoiceOwner)!
  const slot = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => {
    entry.sessionId = sessionId
    entry.disabled = disabled
    if (slot.current) slot.current.appendChild(entry.container)
    owner.refresh()
    return () => {
      if (owner.entries.get(entry.key) === entry) owner.dock.current?.appendChild(entry.container)
      else entry.container.remove()
    }
  }, [entry, sessionId, disabled, owner])
  return (
    <div>
      <div ref={slot} />
      {entry.error && (
        <p role="alert" className="text-sm text-danger">
          {entry.error}
        </p>
      )}
      <PendingSendRecovery entry={entry} />
      {entry.uncertainThread && <Link to={`/s/${entry.uncertainThread}`}>Check conversation</Link>}
    </div>
  )
}

function PendingSendRecovery({ entry }: { entry: Entry }) {
  if (!entry.pending || entry.pending.sending || (!entry.error && !entry.pending.delivering)) return null
  return (
    <div className="flex gap-3 text-sm">
      {entry.error && !entry.pending.delivering && (
        <button
          type="button"
          onClick={() => {
            entry.error = ''
            void deliver(entry)
            notify(entry)
          }}
        >
          Retry send
        </button>
      )}
      <button
        type="button"
        onClick={() => {
          if (entry.pending?.sending) return
          entry.pending = null
          entry.error = ''
          notify(entry)
        }}
      >
        Cancel send
      </button>
    </div>
  )
}

export function VoiceRecorder({
  sessionId,
  disabled,
  onText,
  finalizer,
  onActive,
  onCancel,
  accountId = null,
  background = false,
}: {
  sessionId: string
  disabled: boolean
  onText: (text: string) => void
  finalizer?: RefObject<VoiceFinalizer | null>
  accountId?: string | null
  onActive?: (active: boolean) => void
  onCancel?: () => void
  background?: boolean
}) {
  const [config, setConfig] = useState<Config | null>(null)
  const [phase, setPhase] = useState<Phase>('idle')
  const [error, setError] = useState('')
  const [elapsed, setElapsed] = useState(0)
  const wakeLock = useRef<WakeLockSentinel | null>(null)
  const wakeRequest = useRef(false)
  const wakeRetry = useRef(false)
  const stream = useRef<MediaStream | null>(null)
  const recorder = useRef<MediaRecorder | null>(null)
  const upload = useRef<AbortController | null>(null)
  const audio = useRef<Blob | null>(null)
  const timer = useRef<number | undefined>(undefined)
  const generation = useRef(0)
  const action = useRef<HTMLButtonElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const restoreFocus = useRef(false)
  const onTextRef = useRef(onText)
  onTextRef.current = onText
  const phaseRef = useRef<Phase>('idle')
  const completion = useRef<{ promise: Promise<boolean>; resolve: (ready: boolean) => void } | null>(null)
  const stopRequested = useRef(false)

  function changePhase(next: Phase) {
    phaseRef.current = next
    setPhase(next)
    onActive?.(next !== 'idle')
    if (next === 'error') finish(false)
  }

  function beginFinalization() {
    // The frontend targets ES2020, before Promise.withResolvers.
    let resolve!: (ready: boolean) => void
    const promise = new Promise<boolean>(done => {
      resolve = done
    })
    completion.current = { promise, resolve }
  }

  function finish(ready: boolean) {
    completion.current?.resolve(ready)
    completion.current = null
  }

  function finalize() {
    if (phaseRef.current === 'idle') return Promise.resolve(true)
    if (phaseRef.current === 'error') return Promise.resolve(false)
    stopRequested.current = true
    const token = generation.current
    const pending = completion.current!.promise.then(ready => ready && generation.current === token)
    releaseWakeLock()
    const active = recorder.current
    if (active?.state === 'recording') {
      changePhase('processing')
      active.stop()
    }
    return pending
  }

  useLayoutEffect(() => {
    if (!finalizer) return
    const handle = { finalize }
    finalizer.current = handle
    return () => {
      if (finalizer.current === handle) finalizer.current = null
    }
  })

  async function acquireWakeLock(token: number) {
    if (!navigator.wakeLock || document.visibilityState !== 'visible' || wakeLock.current || wakeRequest.current) return
    wakeRequest.current = true
    wakeRetry.current = false
    try {
      const lock = await navigator.wakeLock.request('screen')
      if (
        generation.current !== token ||
        recorder.current?.state !== 'recording' ||
        document.visibilityState !== 'visible'
      ) {
        await lock.release()
      } else {
        wakeLock.current = lock
        lock.addEventListener('release', () => {
          if (wakeLock.current !== lock) return
          wakeLock.current = null
          if (wakeRetry.current && !wakeRequest.current && recorder.current?.state === 'recording') {
            wakeRetry.current = false
            void acquireWakeLock(generation.current)
          }
        })
      }
    } catch {
      // Wake locks are optional; recording must work without them.
    } finally {
      wakeRequest.current = false
      if ((generation.current !== token || wakeRetry.current) && recorder.current?.state === 'recording') {
        void acquireWakeLock(generation.current)
      }
    }
  }

  function releaseWakeLock() {
    wakeRetry.current = false
    const lock = wakeLock.current
    wakeLock.current = null
    if (lock) void lock.release().catch(() => {})
  }

  function release() {
    releaseWakeLock()
    clearInterval(timer.current)
    timer.current = undefined
    stream.current?.getTracks().forEach(track => track.stop())
    stream.current = null
  }

  function cancel() {
    onCancel?.()
    generation.current++
    finish(false)
    upload.current?.abort()
    upload.current = null
    const active = recorder.current
    recorder.current = null
    if (active && active.state !== 'inactive') active.stop()
    release()
    restoreFocus.current = true
    audio.current = null
    setError('')
    setElapsed(0)
    changePhase('idle')
  }

  useEffect(() => {
    let current = true
    setConfig(null)
    api
      .voiceConfig(sessionId)
      .then(value => {
        if (current) setConfig(value)
      })
      .catch(() => {
        if (current) setConfig(null)
      })
    return () => {
      current = false
    }
  }, [sessionId])
  useLayoutEffect(
    () => () => {
      generation.current++
      finish(false)
      upload.current?.abort()
      const active = recorder.current
      recorder.current = null
      if (active && active.state !== 'inactive') active.stop()
      release()
      audio.current = null
    },
    [sessionId],
  )

  useEffect(() => {
    if (background) return
    if (phase !== 'idle') action.current?.focus()
    else if (restoreFocus.current) {
      restoreFocus.current = false
      trigger.current?.focus()
    }
  }, [phase, background])

  useEffect(() => {
    if (phase === 'idle' || background) return
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        cancel()
      }
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [phase, background])

  useEffect(() => {
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible' && recorder.current?.state === 'recording') {
        if (wakeRequest.current || wakeLock.current) wakeRetry.current = true
        else void acquireWakeLock(generation.current)
      }
    }
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => document.removeEventListener('visibilitychange', onVisibilityChange)
  }, [])

  async function transcribe(blob: Blob, token: number) {
    const controller = new AbortController()
    upload.current = controller
    changePhase('processing')
    try {
      if (accountId) {
        const account = await api.me()
        if (generation.current !== token) return
        if (account.id !== accountId)
          throw new Error('The signed-in account changed. Restore it before retrying transcription.')
      }
      const result = await api.transcribeVoice(sessionId, blob, controller.signal)
      if (generation.current !== token) return
      if (!result.text.trim()) throw new Error('No speech was detected. Retry or dismiss this recording.')
      onTextRef.current(result.text)
      audio.current = null
      upload.current = null
      changePhase('idle')
      finish(true)
    } catch (err) {
      if (generation.current !== token) return
      upload.current = null
      setError(err instanceof Error && err.message ? err.message : 'Transcription failed. Please try again.')
      changePhase('error')
    }
  }

  async function start() {
    if (phaseRef.current !== 'idle' && phaseRef.current !== 'error') return
    beginFinalization()
    stopRequested.current = false
    const token = ++generation.current
    setError('')
    audio.current = null
    setElapsed(0)
    changePhase('permission')
    const mimeType = recordingType()
    if (!mimeType) {
      setError('Your browser does not support a compatible audio recording format.')
      changePhase('error')
      return
    }
    try {
      const media = await navigator.mediaDevices.getUserMedia({ audio: true })
      if (generation.current !== token) {
        media.getTracks().forEach(track => track.stop())
        return
      }
      stream.current = media
      const active = new MediaRecorder(media, { mimeType })
      recorder.current = active
      const chunks: Blob[] = []
      let recordingFailed = false
      active.ondataavailable = event => {
        if (event.data.size) chunks.push(event.data)
      }
      active.onerror = () => {
        if (generation.current !== token) return
        recordingFailed = true
        release()
        if (active.state !== 'inactive') active.stop()
      }
      active.onstop = () => {
        if (generation.current !== token) return
        release()
        recorder.current = null
        const actualType = active.mimeType || mimeType
        const blob = new Blob(chunks, { type: actualType })
        audio.current = blob.size ? blob : null
        if (recordingFailed) {
          setError(
            !audio.current
              ? 'Recording failed without capturing audio. Try recording again.'
              : acceptedType(audio.current.type)
                ? 'Recording failed. Retry transcription or download the captured audio.'
                : 'Recording failed with an unsupported audio format. Download the captured audio.',
          )
          changePhase('error')
          return
        }
        if (!acceptedType(actualType)) {
          setError('Your browser recorded an unsupported audio format.')
          changePhase('error')
          return
        }
        if (!blob.size) {
          setError('No audio was captured. Try recording again.')
          changePhase('error')
          return
        }
        void transcribe(blob, token)
      }
      active.start(1000)
      changePhase('recording')
      void acquireWakeLock(token)
      const started = Date.now()
      timer.current = window.setInterval(() => {
        setElapsed(Math.floor((Date.now() - started) / 1000))
      }, 250)
      if (stopRequested.current) void finalize()
    } catch (err) {
      if (generation.current !== token) return
      release()
      setError(
        err instanceof Error && err.name === 'NotAllowedError'
          ? 'Microphone access was denied. Allow microphone access in your browser and try again.'
          : 'Could not start the microphone. Check your browser permissions and try again.',
      )
      changePhase('error')
    }
  }

  const supported = window.isSecureContext && !!recordingType() && !!navigator.mediaDevices?.getUserMedia
  if (!config?.enabled) return null
  const privacy = 'Voice input (audio sent to OpenAI to transcribe)'
  const buttonClass =
    'flex h-11 w-11 shrink-0 items-center justify-center rounded-control hover:bg-raised focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50'
  const announcement = !supported
    ? 'Voice recording requires a secure connection and a compatible browser.'
    : phase === 'permission'
      ? 'Waiting for microphone permission.'
      : phase === 'recording'
        ? 'Recording. Audio will be sent to OpenAI for transcription.'
        : phase === 'processing'
          ? 'Transcribing audio with OpenAI.'
          : phase === 'error'
            ? error
            : ''

  return (
    <div className="flex shrink-0 items-center text-xs">
      <span className="sr-only" aria-live="polite" aria-atomic="true">
        {announcement}
      </span>
      {phase === 'error' && (
        <span role="alert" className="max-w-xs text-danger">
          {error}
        </span>
      )}
      {background && phase !== 'idle' && phase !== 'error' && <span role="status">{announcement}</span>}
      {phase === 'idle' ? (
        <button
          ref={trigger}
          type="button"
          disabled={disabled || !supported}
          onClick={start}
          aria-label={`Record voice message. ${privacy}`}
          aria-pressed={false}
          title={supported ? privacy : `${privacy}. Requires HTTPS and a compatible browser.`}
          className={`${buttonClass} text-mint`}
        >
          <svg
            width="20"
            height="20"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            aria-hidden="true"
          >
            <rect x="9" y="2" width="6" height="12" rx="3" />
            <path d="M5 11a7 7 0 0 0 14 0M12 18v4M8 22h8" />
          </svg>
        </button>
      ) : phase === 'recording' ? (
        <button
          ref={action}
          type="button"
          aria-label="Stop recording"
          title="Stop recording"
          aria-pressed="true"
          className={buttonClass}
          onClick={() => void finalize()}
        >
          <span aria-hidden="true" className="h-3 w-3 rounded-full bg-red-500 motion-safe:animate-pulse" />
        </button>
      ) : phase === 'error' ? (
        <span
          className="flex h-11 w-11 shrink-0 items-center justify-center text-red-500"
          role="img"
          tabIndex={0}
          aria-label={error}
          title={error}
        >
          ⚠
        </span>
      ) : (
        <span className="flex h-11 w-11 shrink-0 items-center justify-center" aria-hidden="true">
          <span className="inline-block motion-safe:animate-spin">◌</span>
        </span>
      )}
      {phase === 'recording' && (
        <span className="w-11 text-center tabular-nums" aria-hidden="true">
          {String(Math.floor(elapsed / 60)).padStart(2, '0')}:{String(elapsed % 60).padStart(2, '0')}
        </span>
      )}
      {phase === 'error' && (!audio.current || acceptedType(audio.current.type)) && (
        <button
          ref={action}
          type="button"
          className={buttonClass}
          aria-label="Retry"
          title="Retry"
          onClick={() => {
            if (phaseRef.current !== 'error') return
            if (audio.current) {
              beginFinalization()
              void transcribe(audio.current, generation.current)
            } else void start()
          }}
        >
          <span aria-hidden="true">↻</span>
        </button>
      )}
      {phase === 'error' && audio.current && (
        <button
          type="button"
          className={buttonClass}
          aria-label="Download recording"
          title="Download recording"
          onClick={() => {
            const blob = audio.current
            if (!blob) return
            const url = URL.createObjectURL(blob)
            const link = document.createElement('a')
            const type = blob.type.split(';', 1)[0]
            const extension =
              type.includes('mp4') || type.includes('m4a')
                ? 'm4a'
                : type.includes('wav')
                  ? 'wav'
                  : type.includes('mpeg') || type.includes('mp3')
                    ? 'mp3'
                    : type.includes('webm')
                      ? 'webm'
                      : type.includes('ogg')
                        ? 'ogg'
                        : 'bin'
            link.href = url
            link.download = `recording.${extension}`
            link.click()
            window.setTimeout(() => URL.revokeObjectURL(url), 0)
          }}
        >
          <span aria-hidden="true">↓</span>
        </button>
      )}
      {phase !== 'idle' && (
        <button
          ref={
            phase === 'permission' ||
            phase === 'processing' ||
            (phase === 'error' && !!audio.current && !acceptedType(audio.current.type))
              ? action
              : undefined
          }
          type="button"
          className={buttonClass}
          aria-label={phase === 'error' ? 'Dismiss' : 'Cancel'}
          title={phase === 'error' ? 'Dismiss' : 'Cancel'}
          onClick={cancel}
        >
          <span aria-hidden="true">×</span>
        </button>
      )}
    </div>
  )
}
