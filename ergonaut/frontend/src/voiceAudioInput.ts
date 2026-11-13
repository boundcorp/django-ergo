import {
  LocalAudioTrack,
  Room,
  RoomEvent,
  Track,
  TrackEvent,
  type LocalTrackPublication,
  type Participant,
} from 'livekit-client'

export type VoiceAudioInputState = {
  capture: 'unknown' | 'live' | 'disabled' | 'muted' | 'ended' | 'absent'
  publication: 'unknown' | 'published' | 'muted' | 'paused' | 'unpublished'
  error: 'none' | 'permission' | 'missing' | 'busy' | 'capture'
  recovering: boolean
  probe: 'not-observed' | 'sampling' | 'nonzero' | 'no-nonzero-observed' | 'unavailable'
  hardware_source: 'unverified'
}
export const initialVoiceAudioInput: VoiceAudioInputState = {
  capture: 'unknown',
  publication: 'unknown',
  error: 'none',
  recovering: false,
  probe: 'not-observed',
  hardware_source: 'unverified',
}
export function inputError(error: unknown): VoiceAudioInputState['error'] {
  const name = error && typeof error === 'object' && 'name' in error ? error.name : ''
  return name === 'NotAllowedError' || name === 'SecurityError'
    ? 'permission'
    : name === 'NotFoundError' || name === 'DevicesNotFoundError'
      ? 'missing'
      : name === 'NotReadableError' || name === 'TrackStartError'
        ? 'busy'
        : 'capture'
}
export function inputErrorMessage(error: VoiceAudioInputState['error']) {
  return error === 'permission'
    ? 'Microphone permission was denied or blocked. Check browser site permission, secure context and policy, then use Recover microphone.'
    : error === 'missing'
      ? 'No microphone was available. Check your device, then use Recover microphone.'
      : error === 'busy'
        ? 'Microphone capture was unavailable or busy. Check other apps and your device, then use Recover microphone.'
        : error === 'capture'
          ? 'Microphone capture could not be confirmed. Check browser permission and your device, then use Recover microphone.'
          : ''
}

export function bridgeInputEvidence(events: readonly { kind: string; event_id: string }[]) {
  const observed = (stage: string) =>
    events.some(
      event =>
        event.kind === 'bridge_input' &&
        event.event_id.startsWith(`bridge-input:${stage}:`) &&
        event.event_id.length > `bridge-input:${stage}:`.length,
    )
  return {
    speech: observed('speech'),
    forwarded: observed('forwarded'),
    provider_started: observed('provider_started'),
    transcribed: observed('transcribed'),
    provider_error: observed('provider_error'),
  }
}

/** Observes one room only. No capture, restart, device selection or upload occurs in event handlers. */
export class VoiceAudioInput {
  state = { ...initialVoiceAudioInput }
  private disposed = false
  private track?: LocalAudioTrack
  private native?: MediaStreamTrack
  private unbindTrack = () => {}
  private cancelProbe = () => {}
  private unpublished?: LocalTrackPublication
  private recovering = false

  constructor(
    private room: Room,
    private devices: MediaDevices | undefined,
    private changed: (state: VoiceAudioInputState) => void,
  ) {
    room.on(RoomEvent.LocalTrackPublished, this.published)
    room.on(RoomEvent.LocalTrackUnpublished, this.unpublishedTrack)
    room.on(RoomEvent.TrackMuted, this.publicationChanged)
    room.on(RoomEvent.TrackUnmuted, this.publicationChanged)
    room.on(RoomEvent.MediaDevicesError, this.failed)
    room.on(RoomEvent.Reconnecting, this.reconnecting)
    room.on(RoomEvent.Reconnected, this.refresh)
    devices?.addEventListener?.('devicechange', this.deviceChanged)
    this.refresh()
  }
  private update(change: Partial<VoiceAudioInputState>) {
    if (this.disposed) return
    this.state = { ...this.state, ...change }
    this.changed(this.state)
  }
  private published = (publication: LocalTrackPublication) => {
    if (publication.source !== Track.Source.Microphone) return
    this.unpublished = undefined
    this.refresh()
  }
  private unpublishedTrack = (publication: LocalTrackPublication) => {
    if (publication.source !== Track.Source.Microphone) return
    // SDK emits before clearing the publication's track. Don't retain optimistic publication state.
    this.unpublished = publication
    this.refresh()
  }
  private publicationChanged = (_publication: unknown, participant: Participant) => {
    if (participant === this.room.localParticipant) this.refresh()
  }
  private reconnecting = () => {
    this.update({ publication: 'unknown' })
  }
  private deviceChanged = () => {
    this.cancelProbe()
    this.update({ probe: 'not-observed', hardware_source: 'unverified' })
    this.refresh()
  }
  failed = (error: unknown) => {
    this.update({ error: inputError(error) })
    this.refresh()
  }
  refresh = () => {
    if (this.disposed) return
    const publication = this.room.localParticipant.getTrackPublication(Track.Source.Microphone)
    const track = publication?.track instanceof LocalAudioTrack ? publication.track : undefined
    const native = track?.mediaStreamTrack
    if (track !== this.track || native !== this.native) {
      this.cancelProbe()
      this.unbindTrack()
      this.track = track
      this.native = native
      const events = [
        TrackEvent.Muted,
        TrackEvent.Unmuted,
        TrackEvent.Ended,
        TrackEvent.Restarted,
        TrackEvent.UpstreamPaused,
        TrackEvent.UpstreamResumed,
      ] as const
      for (const event of events) track?.on(event, this.refresh)
      for (const event of ['mute', 'unmute', 'ended']) native?.addEventListener(event, this.refresh)
      this.unbindTrack = () => {
        for (const event of events) track?.off(event, this.refresh)
        for (const event of ['mute', 'unmute', 'ended']) native?.removeEventListener(event, this.refresh)
      }
      this.update({ probe: 'not-observed' })
    }
    const capture = !native
      ? 'absent'
      : native.readyState === 'ended'
        ? 'ended'
        : !native.enabled
          ? 'disabled'
          : native.muted
            ? 'muted'
            : 'live'
    const publicationState =
      !publication || publication === this.unpublished
        ? 'unpublished'
        : !track
          ? 'unknown'
          : publication.isMuted
            ? 'muted'
            : track.isUpstreamPaused
              ? 'paused'
              : 'published'
    if (capture !== 'live' || publicationState !== 'published') this.cancelProbe()
    this.update({ capture, publication: publicationState })
  }
  captureSucceeded() {
    this.refresh()
    if (this.state.capture === 'live' && this.state.publication === 'published') this.update({ error: 'none' })
  }
  private ownsCapture(track: LocalTrackPublication['track']) {
    const publication = this.room.localParticipant.getTrackPublication(Track.Source.Microphone)
    return !this.disposed && !!track && publication?.track === track && publication !== this.unpublished
  }
  /** User gesture only: restart the existing track or ask the current SDK participant to recover capture. */
  async recover(intentionallyMuted: boolean) {
    if (this.disposed || this.recovering || intentionallyMuted) return
    const track = this.track
    this.recovering = true
    this.cancelProbe()
    this.update({ recovering: true, probe: 'not-observed', error: 'none' })
    try {
      if (track && this.state.publication !== 'unpublished') {
        await track.restartTrack()
        if (!this.ownsCapture(track)) {
          track.stop()
          return
        }
        if (track.isMuted) await track.unmute()
        if (!this.ownsCapture(track)) track.stop()
      } else {
        const publication = await this.room.localParticipant.setMicrophoneEnabled(true)
        if (publication?.track && !this.ownsCapture(publication.track)) publication.track.stop()
      }
    } catch (error) {
      if (track && !this.ownsCapture(track)) track.stop()
      else if (this.track === track) this.failed(error)
    } finally {
      this.recovering = false
      if (!this.disposed) {
        this.update({ recovering: false })
        this.refresh()
      }
    }
  }
  /** One second of existing-track PCM, never connected to output, recorded, or uploaded. */
  probe() {
    this.refresh()
    this.cancelProbe()
    if (this.disposed || this.state.capture !== 'live' || !this.native) return
    const Audio = window.AudioContext
    if (!Audio || typeof MediaStream === 'undefined') {
      this.update({ probe: 'unavailable' })
      return
    }
    let context: AudioContext | undefined
    let source: MediaStreamAudioSourceNode | undefined
    let analyser: AnalyserNode | undefined
    let poll: ReturnType<typeof setInterval> | undefined
    let timer: ReturnType<typeof setTimeout> | undefined
    let stopped = false
    const finish = (result: VoiceAudioInputState['probe']) => {
      if (stopped) return
      stopped = true
      clearInterval(poll)
      clearTimeout(timer)
      source?.disconnect()
      analyser?.disconnect()
      void context?.close().catch(() => {})
      this.cancelProbe = () => {}
      this.update({ probe: result })
    }
    this.cancelProbe = () => finish('not-observed')
    try {
      context = new Audio()
      source = context.createMediaStreamSource(new MediaStream([this.native]))
      analyser = context.createAnalyser()
      analyser.fftSize = 2048
      source.connect(analyser)
      const samples = new Float32Array(analyser.fftSize)
      this.update({ probe: 'sampling' })
      const sample = () => {
        if (context?.state !== 'running' || !analyser) return
        analyser.getFloatTimeDomainData(samples)
        if (samples.some(value => Number.isFinite(value) && value !== 0)) finish('nonzero')
      }
      void context
        .resume()
        .then(() => {
          if (!stopped) sample()
        })
        .catch(() => finish('unavailable'))
      poll = setInterval(sample, 50)
      timer = setTimeout(() => finish(context?.state === 'running' ? 'no-nonzero-observed' : 'unavailable'), 1000)
    } catch {
      finish('unavailable')
    }
  }
  dispose() {
    this.disposed = true
    this.cancelProbe()
    this.unbindTrack()
    this.room.off(RoomEvent.LocalTrackPublished, this.published)
    this.room.off(RoomEvent.LocalTrackUnpublished, this.unpublishedTrack)
    this.room.off(RoomEvent.TrackMuted, this.publicationChanged)
    this.room.off(RoomEvent.TrackUnmuted, this.publicationChanged)
    this.room.off(RoomEvent.MediaDevicesError, this.failed)
    this.room.off(RoomEvent.Reconnecting, this.reconnecting)
    this.room.off(RoomEvent.Reconnected, this.refresh)
    this.devices?.removeEventListener?.('devicechange', this.deviceChanged)
  }
}
