import { useEffect, useRef, useState } from 'react'
import { Room, RoomEvent, Track } from 'livekit-client'
import { api, type RealtimeVoiceSession } from '../api'

type Phase = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'disconnected'

/** A voice room belongs to this chat. Closing the panel disconnects; ending it is explicit. */
export function RealtimeVoice({
  sessionId,
  disabled,
  onSession,
}: {
  sessionId: string
  disabled: boolean
  onSession?: (session: RealtimeVoiceSession) => void
}) {
  const [enabled, setEnabled] = useState(false)
  const [open, setOpen] = useState(false)
  const [voice, setVoice] = useState<RealtimeVoiceSession | null>(null)
  const [phase, setPhase] = useState<Phase>('idle')
  const [muted, setMuted] = useState(false)
  const [error, setError] = useState('')
  const [ending, setEnding] = useState(false)
  const dialog = useRef<HTMLDialogElement>(null)
  const room = useRef<Room | null>(null)
  const audio = useRef<HTMLDivElement>(null)
  const generation = useRef(0)
  const storageKey = `ergonaut.realtimeVoice.${sessionId}`

  useEffect(() => {
    let alive = true
    api
      .realtimeVoiceConfig()
      .then(config => {
        if (alive) setEnabled(config.enabled)
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [])

  function disconnect() {
    generation.current++
    const current = room.current
    room.current = null
    if (current) void current.disconnect()
    audio.current?.replaceChildren()
    setPhase('disconnected')
    setMuted(false)
  }

  useEffect(
    () => () => {
      generation.current++
      const current = room.current
      room.current = null
      if (current) void current.disconnect()
    },
    [],
  )

  useEffect(() => {
    if (!open || !voice || voice.status !== 'active') return
    let alive = true
    const timer = window.setInterval(() => {
      api
        .realtimeVoiceSession(voice.id)
        .then(session => {
          if (alive) {
            setVoice(session)
            onSession?.(session)
          }
        })
        .catch(() => {})
    }, 3000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [open, voice?.id, voice?.status])

  useEffect(() => {
    if (open && voice?.status === 'ended') {
      sessionStorage.removeItem(storageKey)
      if (room.current) disconnect()
    }
  }, [open, voice?.status, storageKey])
  async function connect() {
    const attempt = ++generation.current
    setError('')
    setPhase('connecting')
    try {
      let session: RealtimeVoiceSession | null = null
      const saved = sessionStorage.getItem(storageKey)
      if (saved) {
        session = await api.realtimeVoiceSession(saved)
        if (session.status !== 'active') {
          sessionStorage.removeItem(storageKey)
          session = null
        }
      }
      if (!session) {
        const sessions = await api.realtimeVoiceSessions(sessionId)
        if (attempt !== generation.current) return
        session = sessions.find(item => item.status === 'active') ?? null
      }
      if (!session) {
        session = await api.startRealtimeVoice(sessionId)
        sessionStorage.setItem(storageKey, session.id)
      }
      if (attempt !== generation.current) return
      onSession?.(session)
      setVoice(session)
      const credentials = await api.realtimeVoiceToken(session.id)
      if (attempt !== generation.current) return
      const current = new Room()
      room.current = current
      current.on(RoomEvent.TrackSubscribed, track => {
        if (track.kind === Track.Kind.Audio && audio.current) audio.current.appendChild(track.attach())
      })
      current.on(RoomEvent.TrackUnsubscribed, track => {
        track.detach().forEach(element => element.remove())
      })
      current.on(RoomEvent.Reconnecting, () => {
        if (room.current === current) setPhase('reconnecting')
      })
      current.on(RoomEvent.Reconnected, () => {
        if (room.current === current) setPhase('connected')
      })
      current.on(RoomEvent.Disconnected, () => {
        if (room.current === current) {
          room.current = null
          audio.current?.replaceChildren()
          setPhase('disconnected')
        }
      })
      await current.connect(credentials.url, credentials.token)
      if (attempt !== generation.current) {
        void current.disconnect()
        return
      }
      await current.localParticipant.setMicrophoneEnabled(true)
      if (attempt !== generation.current) {
        void current.disconnect()
        return
      }
      setPhase('connected')
    } catch (e) {
      if (attempt !== generation.current) return
      disconnect()
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  function show() {
    setOpen(true)
    dialog.current?.showModal()
    void connect()
  }

  function close() {
    disconnect()
    setOpen(false)
    dialog.current?.close()
  }

  async function end() {
    if (!voice) return
    setEnding(true)
    setError('')
    try {
      const ended = await api.endRealtimeVoice(voice.id)
      sessionStorage.removeItem(storageKey)
      setVoice(ended)
      onSession?.(ended)
      disconnect()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setEnding(false)
    }
  }

  async function toggleMute() {
    if (!room.current) return
    try {
      await room.current.localParticipant.setMicrophoneEnabled(muted)
      setMuted(!muted)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  if (!enabled) return null
  return (
    <div className="px-4 py-2 sm:px-6">
      <button
        type="button"
        disabled={disabled}
        onClick={show}
        className="rounded-md border border-stroke px-3 py-1 text-sm text-ink disabled:opacity-50"
      >
        Voice room
      </button>
      <dialog
        ref={dialog}
        aria-labelledby="realtime-voice-title"
        onCancel={event => {
          event.preventDefault()
          close()
        }}
        className="m-auto w-[min(92vw,42rem)] rounded-card border border-stroke bg-surface p-0 text-ink shadow-xl backdrop:bg-black/40"
      >
        <div className="flex items-start gap-4 border-b border-stroke p-4">
          <div>
            <h2 id="realtime-voice-title" className="font-display text-xl font-bold">
              Voice room
            </h2>
            <p className="mt-1 text-sm text-muted" role="status">
              {phase === 'connected'
                ? 'Connected'
                : phase === 'connecting'
                  ? 'Connecting…'
                  : phase === 'reconnecting'
                    ? 'Reconnecting…'
                    : 'Disconnected'}
            </p>
          </div>
          <button
            type="button"
            onClick={close}
            className="ml-auto min-h-11 min-w-11 rounded-control text-muted hover:bg-raised"
            aria-label="Close voice room"
          >
            ×
          </button>
        </div>
        <div className="max-h-[50dvh] overflow-y-auto p-4" aria-live="polite">
          {voice?.events.map(event => (
            <p key={event.event_id} className="mb-3 text-sm">
              <strong>{event.kind === 'user' ? 'You' : event.kind === 'assistant' ? 'Assistant' : event.kind}:</strong>{' '}
              {event.text}
              {event.kind === 'assistant' && (
                <span className="text-muted">
                  {' '}
                  {event.delivery === 'interrupted'
                    ? '(interrupted)'
                    : event.delivery === 'delivered'
                      ? '(playback confirmed; hearing not established)'
                      : '(playback unconfirmed)'}
                </span>
              )}
            </p>
          ))}
          {error && (
            <p className="text-sm text-red-600" role="alert">
              {error}
            </p>
          )}
        </div>
        <div ref={audio} />
        <div className="flex flex-wrap gap-2 border-t border-stroke p-4">
          {phase === 'connected' && (
            <button
              type="button"
              onClick={() => void toggleMute()}
              className="rounded-md border border-stroke px-3 py-2 text-sm"
            >
              {muted ? 'Unmute' : 'Mute'}
            </button>
          )}
          {phase === 'connected' && (
            <button
              type="button"
              onClick={() =>
                void room.current?.startAudio().catch(e => setError(e instanceof Error ? e.message : String(e)))
              }
              className="rounded-md border border-stroke px-3 py-2 text-sm"
            >
              Enable audio
            </button>
          )}
          {phase === 'disconnected' && (
            <button
              type="button"
              onClick={() => void connect()}
              className="rounded-md border border-stroke px-3 py-2 text-sm"
            >
              Reconnect
            </button>
          )}
          {voice?.status === 'active' && (
            <button
              type="button"
              disabled={ending}
              onClick={() => void end()}
              className="rounded-md border border-stroke px-3 py-2 text-sm disabled:opacity-50"
            >
              End room
            </button>
          )}
        </div>
      </dialog>
    </div>
  )
}
