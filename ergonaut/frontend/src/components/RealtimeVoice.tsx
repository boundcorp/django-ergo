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
  const [standbyEnabled, setStandbyEnabled] = useState(false)
  const [open, setOpen] = useState(false)
  const [voice, setVoice] = useState<RealtimeVoiceSession | null>(null)
  const [phase, setPhase] = useState<Phase>('idle')
  const [muted, setMuted] = useState(false)
  const [microphone, setMicrophone] = useState<'enabled' | 'disabled' | 'unknown'>('unknown')
  const [error, setError] = useState('')
  const [ending, setEnding] = useState(false)
  const [action, setAction] = useState<'standby' | 'resume' | 'end' | null>(null)
  const dialog = useRef<HTMLDialogElement>(null)
  const room = useRef<Room | null>(null)
  const audio = useRef<HTMLDivElement>(null)
  const generation = useRef(0)
  const actionLock = useRef(false)
  const revision = useRef(0)
  const storageKey = `ergonaut.realtimeVoice.${sessionId}`

  useEffect(() => {
    let alive = true
    api
      .realtimeVoiceConfig()
      .then(config => {
        if (alive) {
          setEnabled(config.enabled)
          setStandbyEnabled(config.standby_enabled === true)
        }
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
    setMicrophone('unknown')
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
      const observedRevision = ++revision.current
      api
        .realtimeVoiceSession(voice.id)
        .then(session => {
          if (alive && observedRevision === revision.current && !actionLock.current) {
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
  useEffect(() => {
    if (standbyEnabled && voice?.standby_state === 'standby' && room.current) disconnect()
  }, [standbyEnabled, voice?.standby_state])

  async function connect(resumed?: RealtimeVoiceSession) {
    const attempt = ++generation.current
    setError('')
    setPhase('connecting')
    try {
      let session: RealtimeVoiceSession | null = resumed ?? null
      const saved = sessionStorage.getItem(storageKey)
      if (!session && saved) {
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
      if (standbyEnabled && !resumed && session.standby_state && session.standby_state !== 'live') {
        setPhase('disconnected')
        return
      }
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
        if (room.current === current) {
          setPhase('reconnecting')
          setMicrophone('unknown')
        }
      })
      current.on(RoomEvent.Reconnected, () => {
        if (room.current === current) setPhase('connected')
      })
      current.on(RoomEvent.Disconnected, () => {
        if (room.current === current) {
          room.current = null
          audio.current?.replaceChildren()
          setPhase('disconnected')
          setMicrophone('unknown')
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
      setMicrophone('enabled')
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
    if (standbyEnabled && (actionLock.current || (voice?.standby_state === 'requested' && room.current))) return
    disconnect()
    setOpen(false)
    dialog.current?.close()
  }

  async function end() {
    if (!voice || actionLock.current) return
    actionLock.current = true
    revision.current++
    setAction('end')
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
      revision.current++
      actionLock.current = false
      setAction(null)
      setEnding(false)
    }
  }

  async function changeStandby(resume: boolean) {
    if (!voice || actionLock.current) return
    actionLock.current = true
    revision.current++
    setAction(resume ? 'resume' : 'standby')
    setError('')
    try {
      const session = resume ? await api.resumeRealtimeVoice(voice.id) : await api.standbyRealtimeVoice(voice.id)
      setVoice(session)
      onSession?.(session)
      if (resume) await connect(session)
      else if (session.standby_state === 'standby' && room.current) disconnect()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      revision.current++
      actionLock.current = false
      setAction(null)
    }
  }

  async function retryResume() {
    if (!voice || actionLock.current) return
    actionLock.current = true
    revision.current++
    setAction('resume')
    try {
      await connect(voice)
    } finally {
      revision.current++
      actionLock.current = false
      setAction(null)
    }
  }

  async function toggleMute() {
    const current = room.current
    if (!current) return
    try {
      await current.localParticipant.setMicrophoneEnabled(muted)
      if (room.current !== current) return
      setMuted(!muted)
      setMicrophone(muted ? 'enabled' : 'disabled')
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
            {standbyEnabled && voice?.status === 'active' && (
              <div className="mt-1 text-sm text-muted" role="status">
                <p>Microphone: {microphone}</p>
                <p>Provider: {voice.standby_state === 'standby' ? 'closed' : 'unknown'}</p>
                <p>
                  {voice.standby_state === 'requested'
                    ? 'Standby requested; waiting for the bridge to confirm it is safe.'
                    : voice.standby_state === 'standby'
                      ? 'In standby. Resume manually to reconnect.'
                      : voice.standby_state === 'resuming'
                        ? 'Resume requested; provider connection is not confirmed.'
                        : voice.standby_state === 'live'
                          ? 'Stay connected (default).'
                          : 'Standby state unknown.'}
                </p>
              </div>
            )}
            {standbyEnabled && voice?.status === 'ended' && (
              <p className="mt-1 text-sm text-muted" role="status">
                {voice.standby_state === 'requested'
                  ? 'Voice session ended before standby completed. Audio at the closing boundary may have been interrupted.'
                  : 'Voice session ended.'}
              </p>
            )}
          </div>
          <button
            type="button"
            disabled={standbyEnabled && (!!action || (voice?.standby_state === 'requested' && !!room.current))}
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
        {voice?.state?.pending_delegation?.status === 'collecting' && (
          <p className="border-t border-stroke p-4 text-sm" role="status">
            Listening for the end of the background request; nothing submitted yet.
          </p>
        )}
        {['cancelled', 'expired'].includes(voice?.state?.pending_delegation?.status ?? '') && (
          <p className="border-t border-stroke p-4 text-sm" role="status">
            Background request {voice?.state?.pending_delegation?.status}; nothing was submitted.
          </p>
        )}
        <div ref={audio} />
        <div className="flex flex-wrap gap-2 border-t border-stroke p-4">
          {phase === 'connected' && (
            <button
              type="button"
              disabled={standbyEnabled && (!!action || voice?.standby_state === 'requested')}
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
          {phase === 'disconnected' && (!standbyEnabled || !voice?.standby_state || voice.standby_state === 'live') && (
            <button
              type="button"
              onClick={() => void connect()}
              className="rounded-md border border-stroke px-3 py-2 text-sm"
            >
              Reconnect
            </button>
          )}
          {standbyEnabled && voice?.status === 'active' && phase === 'connected' && voice.standby_state === 'live' && (
            <button
              type="button"
              disabled={!!action}
              onClick={() => void changeStandby(false)}
              className="rounded-md border border-stroke px-3 py-2 text-sm disabled:opacity-50"
            >
              Standby while waiting
            </button>
          )}
          {standbyEnabled && voice?.status === 'active' && voice.standby_state === 'standby' && (
            <button
              type="button"
              disabled={!!action}
              onClick={() => void changeStandby(true)}
              className="rounded-md border border-stroke px-3 py-2 text-sm disabled:opacity-50"
            >
              Resume
            </button>
          )}
          {standbyEnabled &&
            voice?.status === 'active' &&
            voice.standby_state === 'resuming' &&
            phase === 'disconnected' && (
              <button
                type="button"
                disabled={!!action}
                onClick={() => void retryResume()}
                className="rounded-md border border-stroke px-3 py-2 text-sm disabled:opacity-50"
              >
                Resume connection
              </button>
            )}
          {voice?.status === 'active' && (
            <button
              type="button"
              disabled={ending || (standbyEnabled && !!action)}
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
