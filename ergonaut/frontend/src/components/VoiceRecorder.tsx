import { useEffect, useRef, useState } from 'react'
import { api } from '../api'

type Config = { enabled: boolean; max_duration: number }
type Phase = 'idle' | 'permission' | 'recording' | 'processing' | 'error'

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
  const [limitReached, setLimitReached] = useState(false)
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

  function release() {
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
    if (phase === 'idle') return
    const onPageHide = () => cancel()
    window.addEventListener('pagehide', onPageHide)
    return () => window.removeEventListener('pagehide', onPageHide)
  }, [phase])

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
    setLimitReached(false)
    setElapsed(0)
    setPhase('permission')
    try {
      const media = await navigator.mediaDevices.getUserMedia({ audio: true })
      if (generation.current !== token) {
        media.getTracks().forEach(track => track.stop())
        return
      }
      stream.current = media
      const active = new MediaRecorder(media)
      recorder.current = active
      const chunks: Blob[] = []
      active.ondataavailable = event => {
        if (event.data.size) chunks.push(event.data)
      }
      active.onerror = () => {
        if (generation.current !== token) return
        generation.current++
        recorder.current = null
        if (active.state !== 'inactive') active.stop()
        release()
        setError('Recording failed. Please try again.')
        setPhase('error')
      }
      active.onstop = () => {
        if (generation.current !== token) return
        release()
        recorder.current = null
        const blob = new Blob(chunks, { type: active.mimeType || 'audio/webm' })
        audio.current = blob
        void transcribe(blob, token)
      }
      active.start()
      setPhase('recording')
      const started = Date.now()
      timer.current = window.setInterval(() => {
        const duration = (Date.now() - started) / 1000
        setElapsed(Math.min(Math.floor(duration), config!.max_duration))
        if (duration >= config!.max_duration && active.state !== 'inactive') {
          setLimitReached(true)
          active.stop()
        }
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

  const supported =
    window.isSecureContext && typeof MediaRecorder !== 'undefined' && !!navigator.mediaDevices?.getUserMedia
  if (!config?.enabled) return null
  if (!supported)
    return (
      <span className="text-xs text-muted">
        <button
          type="button"
          disabled
          aria-label="Record voice message"
          title="Use HTTPS and a browser that supports microphone recording"
        >
          🎙
        </button>{' '}
        Voice recording requires a supported browser and a secure connection (HTTPS).
      </span>
    )

  return (
    <div className="flex flex-wrap items-center gap-2 text-xs" aria-live="polite">
      {phase === 'idle' && (
        <button
          ref={trigger}
          type="button"
          disabled={disabled}
          onClick={start}
          aria-label="Record voice message"
          aria-pressed={false}
          title="Record voice message; audio is sent for remote transcription"
          className="flex min-h-11 min-w-11 items-center justify-center rounded-control text-mint hover:bg-raised focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50"
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
      )}
      {phase === 'permission' && <span>Waiting for microphone permission…</span>}
      {phase === 'recording' && (
        <>
          <span>
            Recording {String(Math.floor(elapsed / 60)).padStart(2, '0')}:{String(elapsed % 60).padStart(2, '0')}. Audio
            will be sent for remote transcription.
          </span>
          <button
            ref={action}
            type="button"
            aria-label="Stop recording"
            aria-pressed={true}
            className="min-h-11 rounded-control px-2"
            onClick={() => recorder.current?.stop()}
          >
            Stop recording
          </button>
        </>
      )}
      {phase === 'processing' && (
        <span>
          {limitReached ? 'Maximum duration reached. ' : ''}
          <span aria-hidden="true" className="inline-block animate-spin">
            ◌
          </span>{' '}
          Transcribing… Audio is sent to the server and provider.
        </span>
      )}
      {phase === 'error' && (
        <>
          <span role="alert">{error}</span>
          <button
            ref={action}
            type="button"
            className="min-h-11 rounded-control px-2"
            onClick={() => (audio.current ? void transcribe(audio.current, generation.current) : void start())}
          >
            Retry
          </button>
        </>
      )}
      {phase !== 'idle' && (
        <button
          ref={phase === 'permission' || phase === 'processing' ? action : undefined}
          type="button"
          className="min-h-11 rounded-control px-2"
          onClick={cancel}
        >
          {phase === 'error' ? 'Dismiss' : 'Cancel'}
        </button>
      )}
    </div>
  )
}
