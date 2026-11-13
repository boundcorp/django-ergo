import {
  createContext,
  useContext,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from 'react'
import { Link, useLocation, useParams } from 'react-router-dom'
import { RemoteAudioTrack, Room, RoomEvent, Track, TrackEvent, type RemoteTrack } from 'livekit-client'
import {
  api,
  type ClientAudioStatus,
  type RealtimeVoiceSession,
  type SessionDetail,
  type VoiceTaskUpdate,
} from '../api'
import { initialVoiceAudioOutput, VoiceAudioOutput, type VoiceAudioOutputState } from '../voiceAudioOutput'
import { VoiceBackgroundWork } from './VoiceSessionCard'
import { collectVoiceAudioDiagnostics, type VoiceAudioDiagnostics } from '../voiceAudioDiagnostics'
import {
  bridgeInputEvidence,
  initialVoiceAudioInput,
  inputErrorMessage,
  VoiceAudioInput,
  type VoiceAudioInputState,
} from '../voiceAudioInput'

type Phase = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'disconnected'
const storageKey = (chatId: string) => `ergonaut.realtimeVoice.${chatId}`
const taskPending = (task: VoiceTaskUpdate) =>
  task.status === 'queued' || task.status === 'delivered' || task.status === 'waiting'

/** Browser playback of the assistant's audio. `none`: no connected call; `waiting`: connected, no
 * attached element confirmed playing; `playing`: an element fired `playing` (hearing still unknown);
 * `blocked`: autoplay policy refused playback; `failed`: any other playback error. */
export type AudioPlayback = 'none' | 'waiting' | 'playing' | 'blocked' | 'failed'
// totalAudioEnergy grows by roughly level² per second; comfort noise stays far below this.
const arrivingEnergy = 1e-5

/** One connected room. Reports and guards key off this identity, so a stale room cannot write. */
type CallAudio = {
  room: Room
  sessionId: string
  live: boolean
  elements: Map<RemoteTrack, HTMLMediaElement>
  playing: Set<HTMLMediaElement>
  problem: 'blocked' | 'failed' | null
  reported: Set<string>
  /** Last totalAudioEnergy per subscribed receiver; inbound audio has arrived once it rises. */
  energy: Map<RemoteAudioTrack, number>
  arriving: boolean
  sampling: boolean
  poll: ReturnType<typeof setInterval> | undefined
}

function audioEventId() {
  const id =
    typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`
  return id.replace(/[^A-Za-z0-9_-]/g, '').slice(0, 80)
}

/** Report detail: the error's name only (e.g. NotAllowedError), never its message. */
function errorName(error: unknown) {
  return typeof error === 'object' && error !== null && 'name' in error && typeof error.name === 'string'
    ? error.name.slice(0, 120)
    : 'Error'
}

/** Final assistant turn ids, from the transcript or (older sessions) assistant events. */
function finalAssistantTurns(voice: RealtimeVoiceSession | null) {
  if (!voice) return []
  return voice.transcript
    ? voice.transcript.filter(turn => turn.role === 'assistant' && turn.is_final).map(turn => turn.turn_id)
    : voice.events.filter(event => event.kind === 'assistant').map(event => event.event_id)
}
type VoiceCallOwner = {
  enabled: boolean | null
  standbyEnabled: boolean
  voice: RealtimeVoiceSession | null
  phase: Phase
  muted: boolean
  microphone: 'enabled' | 'disabled' | 'unknown'
  input: VoiceAudioInputState
  recoverMicrophone: () => Promise<void>
  probeMicrophone: () => void
  audioPlayback: AudioPlayback
  audioArriving: boolean
  unplayedTurn: boolean
  error: string
  errorChatId: string | null
  action: 'start' | 'standby' | 'resume' | 'end' | null
  output: VoiceAudioOutputState
  outputRouting: RefObject<VoiceAudioOutput | null>
  audio: RefObject<HTMLDivElement | null>
  inspect: (session: RealtimeVoiceSession) => void
  start: (chatId: string) => Promise<void>
  end: () => Promise<void>
  changeStandby: (resume: boolean, retry?: boolean) => Promise<void>
  toggleMute: () => Promise<void>
  enableAudio: () => void
  collectAudioDiagnostics: () => VoiceAudioDiagnostics
}

/** One owner per signed-in app. Route changes never tear down a live transport. */
function useVoiceOwner(): VoiceCallOwner {
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [standbyEnabled, setStandbyEnabled] = useState(false)
  const [voice, setVoice] = useState<RealtimeVoiceSession | null>(null)
  const voiceRef = useRef<RealtimeVoiceSession | null>(null)
  const [phase, setPhase] = useState<Phase>('idle')
  const [muted, setMuted] = useState(false)
  const [input, setInput] = useState(initialVoiceAudioInput)
  const inputObserver = useRef<VoiceAudioInput | null>(null)
  const microphone =
    input.capture === 'live' && input.publication === 'published'
      ? 'enabled'
      : input.capture === 'unknown'
        ? 'unknown'
        : 'disabled'
  const [audioPlayback, setAudioPlayback] = useState<AudioPlayback>('none')
  const [audioArriving, setAudioArriving] = useState(false)
  // Final assistant turns already known when this connection went live; later ones were spoken to it.
  const [knownTurns, setKnownTurns] = useState<ReadonlySet<string> | null>(null)
  const call = useRef<CallAudio | null>(null)
  const [error, setError] = useState('')
  const [errorChatId, setErrorChatId] = useState<string | null>(null)
  const [action, setAction] = useState<'start' | 'standby' | 'resume' | 'end' | null>(null)
  const [output, setOutput] = useState(initialVoiceAudioOutput)
  const outputRouting = useRef<VoiceAudioOutput | null>(null)
  const room = useRef<Room | null>(null)
  const audio = useRef<HTMLDivElement>(null)
  const generation = useRef(0)
  const actionLock = useRef(false)
  const connectingChat = useRef<string | null>(null)
  const revision = useRef(0)
  const observationPending = useRef(false)
  const { pathname } = useLocation()

  function update(session: RealtimeVoiceSession) {
    voiceRef.current = session
    setVoice(session)
  }

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
      .catch(() => {
        if (alive) setEnabled(false)
      })
    return () => {
      alive = false
    }
  }, [])

  function endCall() {
    if (call.current) clearInterval(call.current.poll)
    call.current = null
    setAudioPlayback('none')
    setAudioArriving(false)
    setKnownTurns(null)
  }

  /** Fire-and-forget evidence for the current call only; each key is sent at most once per call. */
  function report(target: CallAudio, status: ClientAudioStatus, detail = '', key: string = status) {
    const session = voiceRef.current
    if (call.current !== target || target.reported.has(key)) return
    if (session?.id !== target.sessionId || session.status !== 'active') return
    target.reported.add(key)
    void api
      .reportRealtimeVoiceAudio(target.sessionId, {
        event_id: audioEventId(),
        status,
        ...(detail ? { detail: detail.slice(0, 120) } : {}),
      })
      .catch(() => {
        /* Evidence is best effort; playback never depends on it. */
      })
  }

  function refreshPlayback(target: CallAudio) {
    if (call.current !== target || !target.live) return
    setAudioPlayback(target.playing.size ? 'playing' : (target.problem ?? 'waiting'))
  }

  function playbackProblem(target: CallAudio, error: unknown) {
    if (call.current !== target) return
    const name = errorName(error)
    target.problem = name === 'NotAllowedError' ? 'blocked' : 'failed'
    report(target, target.problem === 'blocked' ? 'playback_blocked' : 'playback_failed', name)
    refreshPlayback(target)
  }

  /** Polls receiver stats about once a second until inbound audio energy rises once. */
  function watchArrival(target: CallAudio) {
    if (target.poll !== undefined || target.arriving) return
    target.poll = setInterval(() => {
      if (call.current !== target) {
        clearInterval(target.poll)
        return
      }
      if (target.sampling) return
      const tracks = [...target.energy.keys()]
      if (!tracks.length) return
      target.sampling = true
      void Promise.all(tracks.map(track => track.getReceiverStats().catch(() => undefined))).then(samples => {
        target.sampling = false
        if (call.current !== target || target.arriving) return
        samples.forEach((sample, index) => {
          const energy = sample?.totalAudioEnergy
          if (energy === undefined || !target.energy.has(tracks[index])) return
          if (energy - (target.energy.get(tracks[index]) ?? 0) > arrivingEnergy) target.arriving = true
          target.energy.set(tracks[index], energy)
        })
        if (!target.arriving) return
        clearInterval(target.poll)
        setAudioArriving(true)
        report(target, 'audio_arriving')
      })
    }, 1000)
  }

  function disconnect() {
    generation.current++
    const current = room.current
    room.current = null
    inputObserver.current?.dispose()
    inputObserver.current = null
    if (current) void current.disconnect()
    audio.current?.replaceChildren()
    outputRouting.current?.clearTracks()
    setPhase('disconnected')
    setMuted(false)
    setInput(initialVoiceAudioInput)
    endCall()
  }

  useEffect(
    () => () => {
      generation.current++
      const current = room.current
      room.current = null
      inputObserver.current?.dispose()
      if (current) void current.disconnect()
      outputRouting.current?.dispose()
      if (call.current) clearInterval(call.current.poll)
    },
    [],
  )

  // Sequential, bounded polling: partial speech is useful only if it arrives promptly.
  // Navigation slows observation, not the call; no overlapping session requests.
  const pendingWork = voice?.delegations?.some(taskPending) || voice?.thread_updates?.some(taskPending)
  useEffect(() => {
    // Keep observing a viewed ended session: already-accepted final text may drain
    // after End's response. Navigation stops this read-only observation unless work is pending.
    if (!voice || (voice.status !== 'active' && !pendingWork && pathname !== `/s/${voice.chat_id}/voice`)) return
    let alive = true
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      if (observationPending.current) {
        if (alive) timer = setTimeout(poll, 750)
        return
      }
      observationPending.current = true
      const observedRevision = revision.current
      try {
        const session = await api.realtimeVoiceSession(voice.id)
        if (alive && observedRevision === revision.current && !actionLock.current && session.chat_id === voice.chat_id)
          update(session)
      } catch {
        /* The last observed state remains visible; do not invent a new SDK state. */
      } finally {
        observationPending.current = false
      }
      if (alive)
        timer = setTimeout(
          poll,
          voice.status === 'active' && document.visibilityState !== 'hidden' && pathname === `/s/${voice.chat_id}/voice`
            ? 750
            : 3000,
        )
    }
    timer = setTimeout(poll, 750)
    return () => {
      alive = false
      clearTimeout(timer)
    }
  }, [voice?.id, voice?.status, pendingWork, pathname])

  useEffect(() => {
    if (voice?.status === 'ended') {
      sessionStorage.removeItem(storageKey(voice.chat_id))
      if (room.current) disconnect()
    }
  }, [voice?.status, voice?.chat_id])
  useEffect(() => {
    if (standbyEnabled && voice?.standby_state === 'standby' && room.current) disconnect()
  }, [standbyEnabled, voice?.standby_state])

  function inspect(session: RealtimeVoiceSession) {
    if (actionLock.current || connectingChat.current || voiceRef.current?.status === 'active') return
    update(session)
  }

  async function connect(chatId: string, resumed?: RealtimeVoiceSession) {
    const attempt = ++generation.current
    setError('')
    setErrorChatId(chatId)
    setPhase('connecting')
    try {
      let session: RealtimeVoiceSession | null =
        resumed ??
        (voiceRef.current?.chat_id === chatId && voiceRef.current.status === 'active' ? voiceRef.current : null)
      const saved = sessionStorage.getItem(storageKey(chatId))
      if (!session && saved) {
        session = await api.realtimeVoiceSession(saved)
        if (session.chat_id !== chatId) throw new Error('Voice session does not belong to this thread.')
        if (session.status !== 'active') {
          sessionStorage.removeItem(storageKey(chatId))
          session = null
        }
      }
      if (!session) {
        const sessions = await api.realtimeVoiceSessions(chatId)
        if (attempt !== generation.current) return
        session = sessions.find(item => item.chat_id === chatId && item.status === 'active') ?? null
      }
      if (!session) session = await api.startRealtimeVoice(chatId)
      if (session.chat_id !== chatId) throw new Error('Voice session does not belong to this thread.')
      if (attempt !== generation.current) return
      sessionStorage.setItem(storageKey(chatId), session.id)
      update(session)
      if (session.status !== 'active') {
        setPhase('disconnected')
        return
      }
      if (standbyEnabled && !resumed && session.standby_state && session.standby_state !== 'live') {
        setPhase('disconnected')
        return
      }
      const credentials = await api.realtimeVoiceToken(session.id)
      if (attempt !== generation.current) return
      const current = new Room()
      room.current = current
      inputObserver.current?.dispose()
      inputObserver.current = new VoiceAudioInput(current, navigator.mediaDevices, next => {
        if (room.current === current && attempt === generation.current && voiceRef.current?.status === 'active')
          setInput(next)
      })
      if (call.current) clearInterval(call.current.poll)
      const target: CallAudio = {
        room: current,
        sessionId: session.id,
        live: false,
        elements: new Map(),
        playing: new Set(),
        problem: null,
        reported: new Set(),
        energy: new Map(),
        arriving: false,
        sampling: false,
        poll: undefined,
      }
      call.current = target
      // The argument is the room's new canPlaybackAudio.
      current.on(RoomEvent.AudioPlaybackStatusChanged, (playing: boolean) => {
        if (room.current !== current) return
        if (!playing) playbackProblem(target, { name: 'NotAllowedError' })
        else if (target.problem === 'blocked') {
          target.problem = null
          refreshPlayback(target)
        }
      })
      current.on(RoomEvent.TrackSubscribed, track => {
        if (room.current !== current || attempt !== generation.current) return
        if (track.kind !== Track.Kind.Audio || !audio.current) return
        // Playback never waits on output routing: a stalled or refused setSinkId only
        // surfaces through the output message while the default route keeps playing.
        const element = document.createElement('audio')
        const key = track.sid || String(target.elements.size)
        const mine = () => call.current === target && target.elements.get(track) === element
        element.addEventListener('playing', () => {
          if (!mine()) return
          target.playing.add(element)
          target.problem = null
          report(target, 'playback_started', '', `playback_started:${key}`)
          refreshPlayback(target)
        })
        const stopped = () => {
          if (!mine()) return
          target.playing.delete(element)
          refreshPlayback(target)
        }
        element.addEventListener('pause', stopped)
        element.addEventListener('ended', stopped)
        element.addEventListener('error', () => {
          if (!mine()) return
          target.playing.delete(element)
          playbackProblem(target, { name: 'MediaError' })
        })
        if (typeof track.on === 'function') {
          track.on(TrackEvent.AudioPlaybackFailed, (e: unknown) => {
            if (mine()) playbackProblem(target, e)
          })
        }
        target.elements.set(track, element)
        if (track instanceof RemoteAudioTrack) target.energy.set(track, 0)
        audio.current.appendChild(track.attach(element))
        void outputRouting.current?.attach(element)
        report(target, 'track_subscribed', '', `track_subscribed:${key}`)
        watchArrival(target)
      })
      current.on(RoomEvent.TrackUnsubscribed, track => {
        if (room.current !== current || attempt !== generation.current) return
        const element = target.elements.get(track)
        target.elements.delete(track)
        if (track instanceof RemoteAudioTrack) target.energy.delete(track)
        if (element) {
          target.playing.delete(element)
          outputRouting.current?.detach(element)
          element.remove()
        }
        track.detach().forEach(element => {
          outputRouting.current?.detach(element)
          element.remove()
        })
        refreshPlayback(target)
      })
      current.on(RoomEvent.Reconnecting, () => {
        if (room.current === current) {
          setPhase('reconnecting')
        }
      })
      current.on(RoomEvent.Reconnected, () => {
        if (room.current === current) {
          setPhase('connected')
          inputObserver.current?.refresh()
          if (!current.canPlaybackAudio) playbackProblem(target, { name: 'NotAllowedError' })
          else refreshPlayback(target)
          void outputRouting.current?.refresh()
        }
      })
      current.on(RoomEvent.Disconnected, () => {
        if (room.current === current) {
          inputObserver.current?.dispose()
          inputObserver.current = null
          room.current = null
          audio.current?.replaceChildren()
          outputRouting.current?.clearTracks()
          setPhase('disconnected')
          setInput(initialVoiceAudioInput)
          endCall()
        }
      })
      await current.connect(credentials.url, credentials.token)
      if (attempt !== generation.current || room.current !== current || voiceRef.current?.status !== 'active') {
        void current.disconnect()
        return
      }
      try {
        const publication = await current.localParticipant.setMicrophoneEnabled(true)
        if (room.current !== current || attempt !== generation.current || voiceRef.current?.status !== 'active')
          publication?.track?.stop()
      } catch (error) {
        // Permission/capture failure does not end or recreate the room, or reset its original deadline.
        if (room.current === current && attempt === generation.current) inputObserver.current?.failed(error)
      }
      if (attempt !== generation.current || room.current !== current || voiceRef.current?.status !== 'active') {
        void current.disconnect()
        return
      }
      setMuted(false)
      inputObserver.current?.refresh()
      setPhase('connected')
      setKnownTurns(new Set(finalAssistantTurns(voiceRef.current)))
      target.live = true
      if (!current.canPlaybackAudio) playbackProblem(target, { name: 'NotAllowedError' })
      else refreshPlayback(target)
      void outputRouting.current?.refresh()
    } catch (e) {
      if (attempt !== generation.current) return
      disconnect()
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  function initializeOutput() {
    if (!outputRouting.current) {
      setOutput(initialVoiceAudioOutput)
      outputRouting.current = new VoiceAudioOutput(document.createElement('audio'), navigator.mediaDevices, setOutput)
      void outputRouting.current.initialize()
    }
  }

  async function start(chatId: string) {
    if (!enabled || actionLock.current || room.current) return
    if (voiceRef.current?.status === 'active' && voiceRef.current.chat_id !== chatId) return
    actionLock.current = true
    connectingChat.current = chatId
    revision.current++
    setAction('start')
    initializeOutput()
    try {
      await connect(chatId)
    } finally {
      revision.current++
      connectingChat.current = null
      actionLock.current = false
      setAction(null)
    }
  }

  async function end() {
    const session = voiceRef.current
    if (!session || actionLock.current) return
    actionLock.current = true
    revision.current++
    setAction('end')
    setError('')
    setErrorChatId(session.chat_id)
    try {
      const ended = await api.endRealtimeVoice(session.id)
      sessionStorage.removeItem(storageKey(session.chat_id))
      update(ended)
      disconnect()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      revision.current++
      actionLock.current = false
      setAction(null)
    }
  }

  async function changeStandby(resume: boolean, retry = false) {
    const session = voiceRef.current
    if (!session || actionLock.current) return
    actionLock.current = true
    revision.current++
    setAction(resume ? 'resume' : 'standby')
    setError('')
    setErrorChatId(session.chat_id)
    try {
      const next = retry
        ? session
        : resume
          ? await api.resumeRealtimeVoice(session.id)
          : await api.standbyRealtimeVoice(session.id)
      update(next)
      if (resume) {
        initializeOutput()
        await connect(session.chat_id, next)
      } else if (next.standby_state === 'standby' && room.current) disconnect()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
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
      inputObserver.current?.refresh()
    } catch (e) {
      if (room.current === current) inputObserver.current?.failed(e)
    }
  }

  async function recoverMicrophone() {
    const current = room.current
    const observer = inputObserver.current
    if (!current || !observer || phase !== 'connected' || voiceRef.current?.status !== 'active' || actionLock.current)
      return
    await observer.recover(muted)
  }

  function probeMicrophone() {
    if (room.current && voiceRef.current?.status === 'active' && phase === 'connected' && !muted)
      inputObserver.current?.probe()
  }

  function enableAudio() {
    const current = room.current
    const target = call.current
    if (!current || target?.room !== current) return
    // Both start inside the click gesture: the room's audio unlock and play() on every attached element.
    const elements = [...target.elements.values()]
    void Promise.all([current.startAudio(), ...elements.map(element => element.play())])
      .then(() => {
        if (call.current !== target) return
        target.problem = null
        for (const element of elements) if (!element.paused && element.isConnected) target.playing.add(element)
        report(target, 'sound_enabled')
        refreshPlayback(target)
      })
      .catch(e => {
        if (call.current !== target) return
        // AbortError: a newer play() or detach superseded this one; nothing failed.
        if (errorName(e) === 'AbortError') return refreshPlayback(target)
        playbackProblem(target, e)
        setError(e instanceof Error ? e.message : String(e))
      })
  }

  // A final assistant turn spoken to this connection while its audio is not playing here.
  const unplayedTurn =
    phase === 'connected' &&
    voice?.status === 'active' &&
    audioPlayback !== 'playing' &&
    knownTurns !== null &&
    finalAssistantTurns(voice).some(id => !knownTurns.has(id))
  useEffect(() => {
    if (unplayedTurn && call.current) report(call.current, 'turn_unplayed')
  }, [unplayedTurn])

  function collectAudioDiagnostics() {
    const track = room.current?.localParticipant.getTrackPublication(Track.Source.Microphone)?.track?.mediaStreamTrack
    return collectVoiceAudioDiagnostics(output, [...(audio.current?.querySelectorAll('audio') ?? [])], track, input)
  }

  return {
    enabled,
    standbyEnabled,
    voice,
    phase,
    muted,
    microphone,
    input,
    recoverMicrophone,
    probeMicrophone,
    audioPlayback,
    audioArriving,
    unplayedTurn,
    error,
    errorChatId,
    action,
    output,
    outputRouting,
    audio,
    inspect,
    start,
    end,
    changeStandby,
    toggleMute,
    enableAudio,
    collectAudioDiagnostics,
  }
}

const VoiceContext = createContext<VoiceCallOwner | null>(null)

export function VoiceCallProvider({ children }: { children: ReactNode }) {
  const owner = useVoiceOwner()
  return (
    <VoiceContext.Provider value={owner}>
      {children}
      <div ref={owner.audio} hidden />
    </VoiceContext.Provider>
  )
}

function useVoiceCall() {
  const owner = useContext(VoiceContext)
  if (!owner) throw new Error('Voice pages require the app voice call owner.')
  return owner
}

export function ActiveVoiceCall() {
  const { voice, action, phase, microphone } = useVoiceCall()
  const { pathname } = useLocation()
  if (voice?.status !== 'active' || pathname === `/s/${voice.chat_id}/voice`) return null
  return (
    <aside
      className="flex flex-wrap items-center justify-between gap-2 border-b border-stroke bg-raised px-4 py-2 text-sm"
      aria-label="Active voice session"
    >
      <span>
        {phase === 'connected'
          ? `Voice call continues · Microphone: ${microphone}`
          : phase === 'connecting'
            ? 'Voice connection starting'
            : phase === 'reconnecting'
              ? 'Voice connection reconnecting'
              : 'Voice session retained · disconnected'}{' '}
        · Thread {voice.chat_id}
        {action ? ` · ${action} pending` : ''}
      </span>
      <Link className="min-h-11 content-center font-semibold underline" to={`/s/${voice.chat_id}/voice`}>
        Return to voice call
      </Link>
    </aside>
  )
}

export function VoiceTranscript({ voice }: { voice: RealtimeVoiceSession | null }) {
  const scroller = useRef<HTMLDivElement>(null)
  const following = useRef(true)
  const [latest, setLatest] = useState(true)
  const turns =
    voice?.transcript ??
    voice?.events
      .filter(event => event.kind === 'user' || event.kind === 'assistant')
      .map(event => ({
        turn_id: event.event_id,
        event_id: event.event_id,
        role: event.kind,
        text: event.text,
        is_final: true,
        delivery: event.delivery,
        created_at: event.created_at,
      })) ??
    []
  // Prefer the final version if a stale partial and its final ever share one response.
  const merged = new Map<string, (typeof turns)[number]>()
  for (const turn of turns) if (!merged.get(turn.turn_id)?.is_final) merged.set(turn.turn_id, turn)
  const signature = [...merged.values()].map(turn => `${turn.turn_id}:${turn.text}:${turn.is_final}`).join('\u0000')
  useLayoutEffect(() => {
    if (following.current && scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight
  }, [signature])
  return (
    <section className="relative flex min-h-[180px] flex-1 flex-col" aria-label="Voice transcript">
      <div
        ref={scroller}
        className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-4 sm:p-6"
        data-voice-transcript
        onScroll={() => {
          const node = scroller.current
          if (!node) return
          following.current = node.scrollHeight - node.scrollTop - node.clientHeight < 64
          setLatest(following.current)
        }}
      >
        {merged.size === 0 && (
          <p className="text-sm text-muted">Your conversation will appear here after you start the call.</p>
        )}
        <div className="mx-auto flex max-w-3xl flex-col gap-4" aria-live="off">
          {[...merged.values()].map(turn => (
            <article
              key={turn.turn_id}
              data-turn-id={turn.turn_id}
              data-final={turn.is_final}
              className={`max-w-[92%] rounded-card border border-stroke px-4 py-3 motion-safe:transition-colors motion-safe:duration-150 ${turn.role === 'user' ? 'ml-auto bg-raised' : 'mr-auto bg-surface'}`}
            >
              <div className="mb-1 text-xs font-semibold text-muted">
                {turn.role === 'user' ? 'You' : 'Assistant'}
                {!turn.is_final && ' · live transcript'}
              </div>
              <p className="whitespace-pre-wrap break-words text-sm leading-relaxed">{turn.text.trim()}</p>
              {turn.role === 'assistant' && turn.is_final && (
                <p className="mt-2 text-xs text-muted">
                  {turn.delivery === 'interrupted'
                    ? 'Interrupted'
                    : turn.delivery === 'delivered'
                      ? 'Playback confirmed; hearing not established'
                      : 'Playback unconfirmed'}
                </p>
              )}
            </article>
          ))}
        </div>
      </div>
      {!latest && (
        <button
          type="button"
          className="absolute bottom-3 left-1/2 min-h-11 -translate-x-1/2 rounded-full border border-stroke bg-surface px-4 text-sm shadow-sm"
          onClick={() => {
            following.current = true
            setLatest(true)
            if (scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight
          }}
        >
          Jump to latest
        </button>
      )}
    </section>
  )
}

/** Dedicated thread page; all transport state lives above routing in VoiceCallProvider. */
export function VoicePage() {
  const { id = '' } = useParams()
  const owner = useVoiceCall()
  const { enabled, standbyEnabled, phase, microphone, muted, action, output, outputRouting } = owner
  const { audioPlayback, audioArriving, unplayedTurn } = owner
  const [loadedDetail, setDetail] = useState<SessionDetail | null>(null)
  const detail = loadedDetail?.session.id === id ? loadedDetail : null
  const [existing, setExisting] = useState<RealtimeVoiceSession | null>(null)
  const [loadError, setLoadError] = useState('')
  const [loading, setLoading] = useState(true)
  const [audioDiagnostics, setAudioDiagnostics] = useState<VoiceAudioDiagnostics | null>(null)
  const inspectRef = useRef(owner.inspect)
  inspectRef.current = owner.inspect
  useEffect(() => {
    let alive = true
    setDetail(null)
    setAudioDiagnostics(null)
    setExisting(null)
    setLoadError('')
    setLoading(true)
    Promise.all([api.session(id), api.realtimeVoiceSessions(id).catch(() => [])])
      .then(([next, sessions]) => {
        if (!alive) return
        if (next.session.id !== id) throw new Error('Loaded thread identity does not match this page.')
        setDetail(next)
        const active = sessions.find(session => session.chat_id === id && session.status === 'active') ?? null
        setExisting(active)
        if (active) inspectRef.current(active)
      })
      .catch(e => {
        if (alive) setLoadError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [id])
  const owns = owner.voice?.chat_id === id
  const voice = owns ? owner.voice : existing?.chat_id === id ? existing : null
  const bridgeInput = bridgeInputEvidence(voice?.events ?? [])
  const blocked = owner.voice?.status === 'active' && !owns
  const closed = detail?.session.status === 'completed' && detail.session.role !== 'thread'
  const pending = ['requested', 'closing'].includes(voice?.standby_state ?? '')
  const runtime =
    owns && phase === 'connected' && voice?.status === 'active' && voice.runtime?.observed_at ? voice.runtime : null
  let context = voice?.briefing?.context || detail?.session.resolved_summary || detail?.session.status_line || ''
  if (!context && detail) {
    for (let index = detail.messages.length - 1; index >= 0 && !context; index--) {
      const message = detail.messages[index]
      if (message.role !== 'user') continue
      context = message.blocks
        .filter(block => block.type === 'text')
        .map(block => block.text)
        .join(' ')
    }
  }
  const contextPreview = context.length > 100 ? `${context.slice(0, 100)}…` : context
  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-surface text-ink">
      <header className="max-h-[28dvh] min-h-0 shrink overflow-y-auto border-b border-stroke px-4 py-2 sm:px-6">
        <Link className="inline-flex min-h-9 items-center text-sm text-muted underline" to={`/s/${id}`}>
          ← Return to thread
        </Link>
        <div className="text-xs font-semibold uppercase tracking-wide text-muted">
          {detail?.session.bot ?? 'Loading bot…'} · Voice
        </div>
        <h1 className="break-words font-display text-xl font-bold sm:text-2xl">
          {detail?.session.title || (loading ? 'Loading thread…' : 'Thread unavailable')}
        </h1>
        <p className="mt-2 text-sm">
          <span className="font-semibold">Context: </span>
          {contextPreview || 'Context is limited to this selected thread.'}
        </p>
        <details className="mt-1 text-sm">
          <summary className="cursor-pointer text-muted">Thread context & call limits</summary>
          <p className="mt-2 max-h-32 overflow-y-auto whitespace-pre-wrap break-words">
            {context ||
              (detail
                ? `${detail.messages.length} recent messages · ${detail.calls.length} recorded calls. Voice will receive the selected thread's context when started.`
                : 'Context is not available yet.')}
          </p>
          <p className="mt-2 text-muted">
            Navigating away does not end an active call. End call stops voice only; background work continues. The
            original 90-second deadline includes standby. Navigation preserves calls within this tab; closing or
            reloading the tab may disconnect the call.
          </p>
        </details>
      </header>
      <div
        className="max-h-[24dvh] min-h-0 shrink overflow-y-auto border-b border-stroke px-4 py-2 text-sm sm:px-6"
        role="status"
      >
        {loadError && (
          <p role="alert" className="text-red-600">
            {loadError}
          </p>
        )}
        {enabled === null ? (
          <p>Checking voice availability…</p>
        ) : (
          !enabled && <p>Voice is unavailable or restricted for this account.</p>
        )}
        {blocked ? (
          <p>
            A call is already active in thread {owner.voice?.chat_id}.{' '}
            <Link className="underline" to={`/s/${owner.voice?.chat_id}/voice`}>
              Return to active call
            </Link>
            . End that call explicitly before starting another.
          </p>
        ) : (
          <>
            <p>
              <strong>
                {standbyEnabled && voice?.standby_state === 'closing'
                  ? 'Disconnecting…'
                  : voice?.status === 'ended'
                    ? 'Call ended'
                    : phase === 'connected'
                      ? 'Connected'
                      : phase === 'connecting'
                        ? 'Connecting…'
                        : phase === 'reconnecting'
                          ? 'Reconnecting…'
                          : voice?.status === 'active'
                            ? 'Disconnected'
                            : 'Ready to start'}
              </strong>{' '}
              · Microphone: {owns ? microphone : 'unknown'}
            </p>
            <p>
              Browser capture: {owns ? owner.input.capture : 'unknown'} · Microphone publication:{' '}
              {owns ? owner.input.publication : 'unknown'}. Published does not prove bridge or provider receipt.
            </p>
            <p>
              Bridge Silero speech: {bridgeInput.speech ? 'observed' : 'not observed'} · Provider send queue:{' '}
              {bridgeInput.forwarded ? 'observed (not remote receipt)' : 'not observed'} · Provider session:{' '}
              {bridgeInput.provider_started ? 'started (not input receipt)' : 'start not observed'} · Transcript:{' '}
              {bridgeInput.transcribed ? 'observed' : 'not observed'}.
            </p>
            {bridgeInput.provider_error && (
              <p>Provider error observed; input receipt is not established by that event.</p>
            )}
            <p>Live/enabled capture does not prove nonzero input. Hardware input source is unverified.</p>
            {owns && owner.input.error !== 'none' && <p role="alert">{inputErrorMessage(owner.input.error)}</p>}
            <p>
              Last observed voice SDK · You: {runtime?.user_state || 'unknown'} · Assistant:{' '}
              {runtime?.agent_state || 'unknown'}
            </p>
            {voice?.briefing?.restriction && (
              <p>
                {voice.briefing.execution_mode === 'normal_thread'
                  ? 'Normal thread execution: task costs are separate from the 90-second voice limit. Normal action approvals still apply.'
                  : 'Restricted read-only mode.'}
              </p>
            )}
            {voice?.events
              .filter(event => event.kind === 'background_status')
              .slice(-1)
              .map(event => (
                <p key={event.event_id} role="status" className="mt-1 font-medium">
                  {event.text}
                </p>
              ))}
            {voice?.briefing && voice.briefing.delegation_available !== true && (
              <p>{voice.briefing.reason || voice.briefing.restriction}</p>
            )}
            {standbyEnabled && voice?.status === 'active' && voice.standby_state !== 'live' && (
              <p>
                {voice.standby_state === 'requested'
                  ? 'Standby requested; waiting for a safe boundary.'
                  : voice.standby_state === 'closing'
                    ? 'Microphone input is becoming unavailable; speech may be cut off.'
                    : voice.standby_state === 'standby'
                      ? 'In standby. Resume manually to reconnect.'
                      : voice.standby_state === 'resuming'
                        ? 'Resume requested; provider connection is not confirmed.'
                        : 'Standby state unknown.'}
              </p>
            )}
            {voice?.state?.standby_audio_uncertain && (
              <p>Speech may have been cut off while disconnecting; lost speech is unknown.</p>
            )}
            {voice?.status === 'ended' && standbyEnabled && pending && (
              <p>Call ended before standby completed; closing-boundary audio may have been interrupted.</p>
            )}
            {owner.errorChatId === id && owner.error && (
              <p className="mt-1 text-red-600" role="alert">
                {owner.error}
              </p>
            )}
          </>
        )}
      </div>
      <VoiceTranscript key={id} voice={voice} />
      {voice && (
        <details
          open={
            [...(voice.delegations ?? []), ...(voice.thread_updates ?? [])].some(
              task => !!task.error || task.status === 'failed' || task.status === 'waiting',
            ) || undefined
          }
          className="max-h-40 min-h-0 shrink overflow-y-auto border-t border-stroke px-4 text-sm"
        >
          <summary className="cursor-pointer py-2 text-muted">
            Background work ·{' '}
            {voice.delegations?.length
              ? voice.delegations.map(task => (task.error ? 'failed' : task.status || 'unknown')).join(', ')
              : voice.state?.pending_delegation?.status || 'nothing submitted'}
          </summary>
          <VoiceBackgroundWork session={voice} />
        </details>
      )}
      <footer className="max-h-[30dvh] min-h-0 shrink overflow-y-auto border-t border-stroke bg-surface px-4 pt-2 pb-[max(0.5rem,env(safe-area-inset-bottom))] sm:px-6">
        <div className="sticky top-0 z-10 flex flex-wrap gap-2 bg-surface [&_button]:min-h-11 [&_button]:rounded-md [&_button]:border [&_button]:border-stroke [&_button]:px-3 [&_button]:py-2 [&_button]:text-sm [&_button]:disabled:opacity-50">
          {(!voice || voice.status !== 'active') && (
            <button
              type="button"
              className="bg-accent text-white"
              disabled={!enabled || !detail || loading || !!loadError || closed || !!blocked || !!action}
              onClick={() => void owner.start(id)}
            >
              Start call
            </button>
          )}
          {!blocked && voice?.status === 'active' && (
            <>
              {phase === 'connected' && (
                <>
                  <button
                    type="button"
                    disabled={!!action || owner.input.recovering || (standbyEnabled && pending)}
                    aria-pressed={muted}
                    onClick={() => void owner.toggleMute()}
                  >
                    {muted ? 'Unmute' : 'Mute'}
                  </button>
                  <button
                    type="button"
                    disabled={!!action || muted || owner.input.recovering || (standbyEnabled && pending)}
                    onClick={() => void owner.recoverMicrophone()}
                  >
                    {owner.input.recovering ? 'Recovering microphone…' : 'Recover microphone'}
                  </button>
                  {audioPlayback !== 'playing' && (
                    <button type="button" onClick={owner.enableAudio} aria-describedby="voice-playback-help">
                      Enable sound
                    </button>
                  )}
                </>
              )}
              {phase !== 'connected' &&
                phase !== 'connecting' &&
                phase !== 'reconnecting' &&
                (!standbyEnabled || !voice.standby_state || voice.standby_state === 'live') && (
                  <button
                    type="button"
                    disabled={!!action || !enabled || !detail || !!loadError}
                    onClick={() => void owner.start(id)}
                  >
                    Start call
                  </button>
                )}
              {standbyEnabled && phase === 'connected' && voice.standby_state === 'live' && (
                <button type="button" disabled={!!action} onClick={() => void owner.changeStandby(false)}>
                  Standby while waiting
                </button>
              )}
              {standbyEnabled && voice.standby_state === 'standby' && (
                <button
                  type="button"
                  disabled={!!action || !enabled || !detail || !!loadError}
                  onClick={() => void owner.changeStandby(true)}
                >
                  Resume
                </button>
              )}
              {standbyEnabled && voice.standby_state === 'resuming' && phase !== 'connected' && (
                <button
                  type="button"
                  disabled={!!action || !enabled || !detail || !!loadError}
                  onClick={() => void owner.changeStandby(true, true)}
                >
                  Resume connection
                </button>
              )}
              <button type="button" className="text-danger" disabled={!!action} onClick={() => void owner.end()}>
                End call
              </button>
            </>
          )}
        </div>
        <p className="mt-1 text-xs text-muted">End call stops voice only; background work continues.</p>
        {owns && phase === 'connected' && (unplayedTurn || (audioArriving && audioPlayback !== 'playing')) && (
          <p role="alert" className="mt-1 text-sm font-medium text-red-600">
            The assistant spoke, but this browser has not started playing its audio. Tap Enable sound.
          </p>
        )}
        {phase === 'connected' && audioPlayback !== 'playing' && (
          <p id="voice-playback-help" className="mt-1 text-xs text-muted">
            {audioPlayback === 'blocked'
              ? 'Your browser blocked playback.'
              : audioPlayback === 'failed'
                ? "This browser could not play the assistant's audio."
                : "The assistant's audio hasn't started playing in this browser yet. Tap Enable sound."}{' '}
            Enable sound allows this call's audio; it does not join again or change the microphone.
          </p>
        )}
        {phase === 'connected' && audioPlayback === 'playing' && audioArriving && (
          <p className="mt-1 text-xs text-muted">
            Assistant audio is playing in this browser. If you can't hear it, raise the volume during the call (Android
            uses the call volume) or check the speaker/Bluetooth route.
          </p>
        )}
        {phase !== 'connected' && (
          <p className="mt-1 text-xs text-muted">Start call connects voice and requests microphone access.</p>
        )}
        <section className="mt-1 text-sm" aria-label="Audio output">
          {outputRouting.current && output.message && (
            <p className="mt-1 text-xs text-muted" role="status">
              {output.message}
            </p>
          )}
          <details className="mt-2 text-xs text-muted">
            <summary className="cursor-pointer py-2">Diagnostics</summary>
            <p className="mt-1 break-all">
              Thread ID: {id} · Owner: {detail?.session.username || 'unknown'}
            </p>
            <p className="mt-1 break-all">
              Voice session: {voice?.id || 'not started'} · Transport: {phase}
            </p>
            <p className="mt-1">
              Browser playback:{' '}
              {audioPlayback === 'none'
                ? 'not connected'
                : audioPlayback === 'waiting'
                  ? 'not started yet'
                  : audioPlayback === 'playing'
                    ? 'playing; hearing unconfirmed'
                    : audioPlayback === 'blocked'
                      ? 'blocked by the browser'
                      : 'failed'}{' '}
              · Assistant audio arriving: {audioArriving ? 'yes' : 'no'} · Provider:{' '}
              {standbyEnabled && voice?.standby_state === 'standby' ? 'closed' : 'unknown'}
            </p>
            {runtime?.observed_at && (
              <p className="mt-1">
                Observed{' '}
                <time dateTime={runtime.observed_at}>{new Date(runtime.observed_at).toLocaleTimeString()}</time>; not a
                bridge heartbeat.
              </p>
            )}
            {voice?.briefing?.restriction && <p className="mt-1">{voice.briefing.restriction}</p>}
            <h2 className="mt-3 font-semibold">Audio route diagnostics</h2>
            {output.support === 'available' && (
              <div className="flex flex-wrap items-center gap-2">
                <label htmlFor="voice-audio-output">Audio output</label>
                <select
                  id="voice-audio-output"
                  value={output.applied ?? '__unknown__'}
                  disabled={output.busy}
                  onChange={event => void outputRouting.current?.select(event.target.value)}
                  className="min-h-11 max-w-full rounded-md border border-stroke bg-surface px-2"
                >
                  <option value="">System default (phone/OS controlled)</option>
                  {output.applied === null && <option value="__unknown__">Output unknown</option>}
                  {output.applied && !output.devices.some(device => device.deviceId === output.applied) && (
                    <option value={output.applied}>Previously selected output (availability unknown)</option>
                  )}
                  {output.devices.map(device => (
                    <option key={device.deviceId} value={device.deviceId}>
                      {device.label}
                    </option>
                  ))}
                </select>
                {output.chooser && (
                  <button
                    type="button"
                    disabled={output.busy}
                    onClick={() => void outputRouting.current?.choose()}
                    className="min-h-11 rounded-md border border-stroke px-3"
                  >
                    Choose another output
                  </button>
                )}
                <button
                  type="button"
                  disabled={output.busy}
                  onClick={() => void outputRouting.current?.refresh()}
                  className="min-h-11 rounded-md border border-stroke px-3"
                >
                  Refresh outputs
                </button>
              </div>
            )}
            {outputRouting.current && output.support === 'available' && (
              <p className="mt-1 text-xs text-muted">
                Browser selection only; physical audio output and hearing are unconfirmed.
              </p>
            )}
            <p className="mt-1">
              Microphone capture can use a different communication route from media playback, even with system default
              selected. Physical Bluetooth routing is unknown; diagnostics do not change your route.
            </p>
            <h2 className="mt-3 font-semibold">Local input check</h2>
            <p>
              Recovery requests microphone access on this existing call; it may prompt for permission. It does not
              create another room or extend the call deadline. Unmute intentionally muted input yourself first.
            </p>
            <button
              type="button"
              className="my-1 min-h-11 rounded-md border border-stroke px-3"
              disabled={
                !owns ||
                phase !== 'connected' ||
                muted ||
                owner.input.capture !== 'live' ||
                owner.input.probe === 'sampling'
              }
              onClick={owner.probeMicrophone}
            >
              Check existing microphone locally
            </button>
            <p>
              Local input check: {owns ? owner.input.probe : 'not-observed'}. Checks one second of the existing track
              only; no new capture, audio playback, recording, device identifiers or upload. Nonzero samples do not
              identify the hardware source or prove provider receipt.
            </p>
            <button
              type="button"
              className="my-1 min-h-11 rounded-md border border-stroke px-3"
              onClick={() => setAudioDiagnostics(owner.collectAudioDiagnostics())}
            >
              Collect route diagnostics
            </button>
            <p>No capture, room connection, device names or device IDs. Snapshot changes only when collected again.</p>
            <p>
              Browser family/version reflects the Chromium engine when applicable; it does not identify Brave or its
              official release version.
            </p>
            {audioDiagnostics && (
              <pre
                className="mt-1 max-h-36 overflow-auto whitespace-pre-wrap break-words"
                aria-label="Route diagnostic snapshot"
              >
                {JSON.stringify(audioDiagnostics, null, 2)}
              </pre>
            )}
            <h2 className="mt-3 font-semibold">Runtime events</h2>
            <div className="mt-1 max-h-36 overflow-y-auto">
              {voice?.events
                .filter(event => event.kind !== 'user' && event.kind !== 'assistant')
                .map(event => (
                  <p key={event.event_id} className="mt-1 whitespace-pre-wrap break-words">
                    <time dateTime={event.created_at}>{new Date(event.created_at).toLocaleTimeString()}</time>{' '}
                    {event.kind}: {event.text}
                  </p>
                ))}
              {!voice?.events.some(event => event.kind !== 'user' && event.kind !== 'assistant') && (
                <p>No runtime events recorded.</p>
              )}
            </div>
            <p className="mt-2">
              Browser selection only; hearing and the physical audio route are not confirmed. If an output is missing,
              use your phone or operating system audio controls. Microphone access starts only after Start call, Resume,
              Unmute or Recover microphone.
            </p>
          </details>
        </section>
      </footer>
    </div>
  )
}
