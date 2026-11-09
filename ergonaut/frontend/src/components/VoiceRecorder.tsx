import { useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'
import { api } from '../api'

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

/** Insert dictated text at the composer's selection, just like typing. */
export function ComposerVoice({
  sessionId,
  disabled,
  composer,
  setText,
}: {
  sessionId: string
  disabled: boolean
  composer: RefObject<HTMLTextAreaElement | null>
  setText: (text: string | ((current: string) => string)) => void
}) {
  return (
    <VoiceRecorder
      key={sessionId}
      sessionId={sessionId}
      disabled={disabled}
      onText={transcript => {
        const input = composer.current
        if (!input) return
        const start = input.selectionStart
        const end = input.selectionEnd
        setText(current => current.slice(0, start) + transcript + current.slice(end))
        requestAnimationFrame(() => {
          input.focus()
          input.setSelectionRange(start + transcript.length, start + transcript.length)
        })
      }}
    />
  )
}

export function VoiceRecorder({
  sessionId,
  disabled,
  onText,
}: {
  sessionId: string
  disabled: boolean
  onText: (text: string) => void
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
    generation.current++
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
    setPhase('idle')
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
  useEffect(
    () => () => {
      generation.current++
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
    if (phase !== 'idle') action.current?.focus()
    else if (restoreFocus.current) {
      restoreFocus.current = false
      trigger.current?.focus()
    }
  }, [phase])

  useEffect(() => {
    if (phase === 'idle') return
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        cancel()
      }
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [phase])

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
    setPhase('processing')
    try {
      const result = await api.transcribeVoice(sessionId, blob, controller.signal)
      if (generation.current !== token) return
      if (!result.text.trim()) throw new Error('No speech was detected. Retry or dismiss this recording.')
      audio.current = null
      upload.current = null
      setPhase('idle')
      onTextRef.current(result.text)
    } catch (err) {
      if (generation.current !== token) return
      upload.current = null
      setError(err instanceof Error && err.message ? err.message : 'Transcription failed. Please try again.')
      setPhase('error')
    }
  }

  async function start() {
    const token = ++generation.current
    setError('')
    audio.current = null
    setElapsed(0)
    setPhase('permission')
    const mimeType = recordingType()
    if (!mimeType) {
      setError('Your browser does not support a compatible audio recording format.')
      setPhase('error')
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
          setPhase('error')
          return
        }
        if (!acceptedType(actualType)) {
          setError('Your browser recorded an unsupported audio format.')
          setPhase('error')
          return
        }
        if (!blob.size) {
          setError('No audio was captured. Try recording again.')
          setPhase('error')
          return
        }
        void transcribe(blob, token)
      }
      active.start(1000)
      setPhase('recording')
      void acquireWakeLock(token)
      const started = Date.now()
      timer.current = window.setInterval(() => {
        setElapsed(Math.floor((Date.now() - started) / 1000))
      }, 250)
    } catch (err) {
      if (generation.current !== token) return
      release()
      setError(
        err instanceof Error && err.name === 'NotAllowedError'
          ? 'Microphone access was denied. Allow microphone access in your browser and try again.'
          : 'Could not start the microphone. Check your browser permissions and try again.',
      )
      setPhase('error')
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
          onClick={() => {
            releaseWakeLock()
            recorder.current?.stop()
          }}
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
          onClick={() => (audio.current ? void transcribe(audio.current, generation.current) : void start())}
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
