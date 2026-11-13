import { afterEach, beforeEach, expect, spyOn, test } from 'bun:test'
import { Window } from 'happy-dom'
import { LocalAudioTrack, Room, RoomEvent, Track, TrackEvent, type LocalTrackPublication } from 'livekit-client'
import { bridgeInputEvidence, VoiceAudioInput, type VoiceAudioInputState } from './voiceAudioInput'

let window: Window
let room: Room
let input: VoiceAudioInput
let native: MediaStreamTrack
let track: LocalAudioTrack
let publication: LocalTrackPublication | undefined
let state: VoiceAudioInputState
let restorePublication = () => {}
const previous = {
  window: globalThis.window,
  navigator: globalThis.navigator,
  document: globalThis.document,
  MediaStream: globalThis.MediaStream,
}
let captures = 0
let restarts = 0
let devices: EventTarget

beforeEach(async () => {
  window = new Window({ url: 'https://localhost' })
  class SyntheticStream {
    constructor(private tracks: MediaStreamTrack[]) {}
    getTracks() {
      return this.tracks
    }
    getAudioTracks() {
      return this.tracks
    }
  }
  Object.assign(globalThis, {
    window,
    navigator: window.navigator,
    document: window.document,
    MediaStream: SyntheticStream,
  })
  captures = 0
  restarts = 0
  devices = new window.EventTarget() as unknown as EventTarget
  Object.assign(devices, {
    getUserMedia: () => {
      captures++
      throw new Error('No capture in observer or probe')
    },
  })
  native = Object.assign(new window.EventTarget(), {
    id: 'synthetic-only',
    kind: 'audio',
    enabled: true,
    muted: false,
    readyState: 'live',
    getConstraints: () => ({}),
    getSettings: () => ({ deviceId: 'PRIVATE' }),
    stop() {},
  }) as unknown as MediaStreamTrack
  track = new LocalAudioTrack(native)
  track.source = Track.Source.Microphone
  await Promise.resolve()
  track.restartTrack = async () => {
    restarts++
    track.emit(TrackEvent.Restarted, track)
  }
  publication = {
    source: Track.Source.Microphone,
    track,
    get isMuted() {
      return track.isMuted
    },
  } as LocalTrackPublication
  room = new Room()
  const get = spyOn(room.localParticipant, 'getTrackPublication').mockImplementation(() => publication)
  restorePublication = () => get.mockRestore()
  input = new VoiceAudioInput(room, devices as unknown as MediaDevices, next => {
    state = next
  })
})
afterEach(() => {
  input.dispose()
  track.stop()
  restorePublication()
  Object.assign(globalThis, previous)
  window.happyDOM.abort()
})
function nativeState(change: Partial<Pick<MediaStreamTrack, 'readyState' | 'muted' | 'enabled'>>, event: string) {
  Object.assign(native, change)
  native.dispatchEvent(new window.Event(event) as unknown as Event)
}

test('native mute/end, SDK mute/upstream and unpublish replace optimistic enabled state', async () => {
  expect(state.capture).toBe('live')
  expect(state.publication).toBe('published')
  nativeState({ muted: true }, 'mute')
  expect(state.capture).toBe('muted')
  nativeState({ muted: false }, 'unmute')
  await track.mute()
  expect(state.capture).toBe('disabled')
  expect(state.publication).toBe('muted')
  await track.unmute()
  expect(state.capture).toBe('live')
  nativeState({ readyState: 'ended' }, 'ended')
  expect(state.capture).toBe('ended')
  room.emit(RoomEvent.LocalTrackUnpublished, publication!, room.localParticipant)
  expect(state.publication).toBe('unpublished')
  expect(captures).toBe(0)
  expect(restarts).toBe(0)
})

test('reconnect re-observes current capture; devicechange never restarts or unmutes', async () => {
  room.emit(RoomEvent.Reconnecting)
  expect(state.publication).toBe('unknown')
  room.emit(RoomEvent.Reconnected)
  expect(state.capture).toBe('live')
  expect(state.publication).toBe('published')
  await track.mute()
  devices.dispatchEvent(new window.Event('devicechange') as unknown as Event)
  expect(track.isMuted).toBe(true)
  expect(native.enabled).toBe(false)
  expect(state.hardware_source).toBe('unverified')
  await input.recover(true)
  expect(restarts).toBe(0)
  expect(captures).toBe(0)
})

test('only explicit recovery restarts existing SDK track and errors are sanitized', async () => {
  nativeState({ readyState: 'ended' }, 'ended')
  expect(restarts).toBe(0)
  await input.recover(false)
  expect(restarts).toBe(1)
  track.restartTrack = async () => {
    throw Object.assign(new Error('PRIVATE DEVICE'), { name: 'NotAllowedError' })
  }
  await input.recover(false)
  expect(state.error).toBe('permission')
  expect(JSON.stringify(state)).not.toContain('PRIVATE')
  expect(captures).toBe(0)
})

test('disposal removes room/native/device listeners and ignores late recovery failure', async () => {
  const { promise, reject } = Promise.withResolvers<void>()
  track.restartTrack = () => promise
  const recovery = input.recover(false)
  input.dispose()
  const previousState = state
  nativeState({ readyState: 'ended' }, 'ended')
  room.emit(RoomEvent.MediaDevicesError, new Error('late'))
  devices.dispatchEvent(new window.Event('devicechange') as unknown as Event)
  reject(new Error('late recovery'))
  await recovery
  expect(state).toBe(previousState)
  expect(room.listenerCount(RoomEvent.LocalTrackPublished)).toBe(0)
})

test('explicit local probe consumes only existing-track synthetic PCM and closes without output or upload', async () => {
  let contexts = 0
  let closed = 0
  let sourceTracks: MediaStreamTrack[] = []
  let outputConnections = 0
  const analyser = {
    fftSize: 0,
    getFloatTimeDomainData(samples: Float32Array) {
      samples.fill(0.25)
    },
    disconnect() {},
  }
  class SyntheticContext {
    state = 'running'
    constructor() {
      contexts++
    }
    createMediaStreamSource(stream: MediaStream) {
      sourceTracks = stream.getTracks()
      return {
        connect(target: unknown) {
          if (target !== analyser) outputConnections++
        },
        disconnect() {},
      }
    }
    createAnalyser() {
      return analyser
    }
    async resume() {}
    async close() {
      closed++
    }
  }
  Object.defineProperty(window, 'AudioContext', { configurable: true, value: SyntheticContext })
  expect(contexts).toBe(0)
  input.probe()
  await Promise.resolve()
  expect(state.probe).toBe('nonzero')
  expect(sourceTracks).toEqual([native])
  expect(closed).toBe(1)
  expect(outputConnections).toBe(0)
  expect(captures).toBe(0)
  devices.dispatchEvent(new window.Event('devicechange') as unknown as Event)
  expect(state.probe).toBe('not-observed')
})

test('live/enabled track does not imply nonzero input and unavailable analyser is explicit', () => {
  expect(state.capture).toBe('live')
  expect(state.probe).toBe('not-observed')
  input.probe()
  expect(state.probe).toBe('unavailable')
  expect(captures).toBe(0)
})

test('same-participant recovery after denied initial capture requires explicit action', async () => {
  publication = undefined
  input.failed(Object.assign(new Error('PRIVATE'), { name: 'NotAllowedError' }))
  let sdkRecoveries = 0
  const recover = spyOn(room.localParticipant, 'setMicrophoneEnabled').mockImplementation(async () => {
    sdkRecoveries++
    publication = { source: Track.Source.Microphone, track, isMuted: false } as LocalTrackPublication
    room.emit(RoomEvent.LocalTrackPublished, publication, room.localParticipant)
    return publication
  })
  try {
    expect(state.capture).toBe('absent')
    expect(state.error).toBe('permission')
    devices.dispatchEvent(new window.Event('devicechange') as unknown as Event)
    room.emit(RoomEvent.Reconnected)
    expect(sdkRecoveries).toBe(0)
    await input.recover(false)
    expect(sdkRecoveries).toBe(1)
    expect(state.capture).toBe('live')
    expect(state.error).toBe('none')
  } finally {
    recover.mockRestore()
  }
})

test('bridge evidence requires stage IDs and never upgrades local publication or provider-start to receipt', () => {
  const event = (stage: string, kind = 'bridge_input') => ({ kind, event_id: `bridge-input:${stage}:claim` })
  expect(bridgeInputEvidence([event('provider_started')])).toEqual({
    speech: false,
    forwarded: false,
    provider_started: true,
    transcribed: false,
    provider_error: false,
  })
  expect(bridgeInputEvidence([event('forwarded'), event('transcribed', 'user')]).transcribed).toBe(false)
  expect(bridgeInputEvidence([event('speech'), event('transcribed')])).toEqual({
    speech: true,
    forwarded: false,
    provider_started: false,
    transcribed: true,
    provider_error: false,
  })
  expect(bridgeInputEvidence([{ kind: 'bridge_input', event_id: 'bridge-input:speech:' }]).speech).toBe(false)
})

test('silent existing-track PCM reports only no nonzero observed and releases analyser resources', async () => {
  let finish: (() => void) | undefined
  let closed = 0
  const analyser = {
    fftSize: 0,
    getFloatTimeDomainData(samples: Float32Array) {
      samples.fill(0)
    },
    disconnect() {},
  }
  class SilentContext {
    state = 'running'
    createMediaStreamSource() {
      return { connect() {}, disconnect() {} }
    }
    createAnalyser() {
      return analyser
    }
    async resume() {}
    async close() {
      closed++
    }
  }
  Object.defineProperty(window, 'AudioContext', { configurable: true, value: SilentContext })
  const timeout = spyOn(globalThis, 'setTimeout').mockImplementation(((callback: () => void) => {
    finish = callback
    return 1
  }) as typeof setTimeout)
  const interval = spyOn(globalThis, 'setInterval').mockImplementation((() => 1) as typeof setInterval)
  try {
    input.probe()
    await Promise.resolve()
    expect(state.probe).toBe('sampling')
    finish!()
    expect(state.probe).toBe('no-nonzero-observed')
    expect(state.capture).toBe('live')
    expect(closed).toBe(1)
    expect(captures).toBe(0)
  } finally {
    timeout.mockRestore()
    interval.mockRestore()
  }
})

test('late existing-track recovery completion stops capture after observer disposal', async () => {
  const pending = Promise.withResolvers<void>()
  const stop = spyOn(track, 'stop')
  track.restartTrack = () => pending.promise
  try {
    const recovery = input.recover(false)
    input.dispose()
    const previousState = state
    pending.resolve()
    await recovery
    expect(stop).toHaveBeenCalledTimes(1)
    expect(state).toBe(previousState)
  } finally {
    stop.mockRestore()
  }
})

test('late SDK recovery publication is stopped after disposal and missing track is never published evidence', async () => {
  publication = { source: Track.Source.Microphone, isMuted: false } as LocalTrackPublication
  input.refresh()
  expect(state.publication).toBe('unknown')
  const pending = Promise.withResolvers<LocalTrackPublication | undefined>()
  const recover = spyOn(room.localParticipant, 'setMicrophoneEnabled').mockImplementation(() => pending.promise)
  const stop = spyOn(track, 'stop')
  try {
    const recovery = input.recover(false)
    input.dispose()
    pending.resolve({ source: Track.Source.Microphone, track } as LocalTrackPublication)
    await recovery
    expect(stop).toHaveBeenCalledTimes(1)
  } finally {
    recover.mockRestore()
    stop.mockRestore()
  }
})
