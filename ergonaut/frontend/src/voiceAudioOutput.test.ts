import { expect, test } from 'bun:test'
import { initialVoiceAudioOutput, VoiceAudioOutput, type VoiceAudioOutputState } from './voiceAudioOutput'

type MockSink = HTMLMediaElement & {
  sinkId: string
  calls: string[]
  setSinkId: (deviceId: string) => Promise<void>
}

function sink() {
  const value = {
    sinkId: '',
    calls: [] as string[],
    async setSinkId(deviceId: string) {
      value.calls.push(deviceId)
      value.sinkId = deviceId
    },
  }
  return value as MockSink
}

function device(deviceId: string, label = '') {
  return { kind: 'audiooutput', deviceId, label, groupId: '', toJSON: () => ({}) } as MediaDeviceInfo
}

function media() {
  const listeners = new Set<() => void>()
  const mock = {
    outputs: [device('default', 'Default'), device('bluetooth', 'Bluetooth headset'), device('speaker', 'Speakers')],
    captures: 0,
    enumerations: 0,
    async enumerateDevices() {
      mock.enumerations++
      return mock.outputs
    },
    async getUserMedia() {
      mock.captures++
      throw new Error('Capture must not be used by output controls')
    },
    addEventListener(_event: string, listener: () => void) {
      listeners.add(listener)
    },
    removeEventListener(_event: string, listener: () => void) {
      listeners.delete(listener)
    },
    change() {
      listeners.forEach(listener => listener())
    },
    listeners,
  }
  return mock as typeof mock & MediaDevices & { selectAudioOutput?: () => Promise<MediaDeviceInfo> }
}

function setup(probe = sink(), devices: MediaDevices | null = media()) {
  const states: VoiceAudioOutputState[] = []
  const router = new VoiceAudioOutput(probe, devices ?? undefined, state => states.push(state))
  return {
    probe,
    router,
    states,
    get state() {
      return states[states.length - 1] ?? initialVoiceAudioOutput
    },
  }
}

test('unknown/no media APIs cannot expose an output selector or request capture', async () => {
  const noApis = {} as HTMLMediaElement
  const routing = setup(noApis as MockSink, null)
  await routing.router.initialize()
  expect(routing.state.support).toBe('unavailable')
  expect(routing.state.applied).toBeNull()
  expect(routing.state.chooser).toBe(false)
  expect(routing.state.message).toContain('phone or operating system')
})

test('method presence is not support when the silent default probe rejects', async () => {
  const probe = sink()
  probe.setSinkId = async () => {
    throw new DOMException('Blocked', 'NotAllowedError')
  }
  const routing = setup(probe)
  await routing.router.initialize()
  expect(routing.state.support).toBe('unavailable')
  expect(routing.state.message).toContain('permission was denied or blocked')
  await routing.router.select('bluetooth')
  expect(routing.state.applied).toBeNull()
})

test('a working silent sink probe exposes only output devices and never captures', async () => {
  const devices = media()
  devices.outputs.push({ ...device('microphone'), kind: 'audioinput' })
  const routing = setup(sink(), devices)
  await routing.router.initialize()
  expect(routing.state.support).toBe('available')
  expect(routing.state.applied).toBe('')
  expect(routing.state.chooser).toBe(false)
  expect(routing.state.devices.map(item => item.deviceId)).toEqual(['bluetooth', 'speaker'])
  expect(devices.captures).toBe(0)
})

test('setSinkId can work without enumerateDevices or selectAudioOutput', async () => {
  const routing = setup(sink(), {} as MediaDevices)
  await routing.router.initialize()
  expect(routing.state.support).toBe('available')
  expect(routing.state.chooser).toBe(false)
  expect(routing.state.applied).toBe('')
  expect(routing.state.message).toContain('cannot list audio outputs')
})

test('a picker without a sink API does not claim it can route playback', async () => {
  const devices = media()
  let choices = 0
  devices.selectAudioOutput = async () => {
    choices++
    return device('bluetooth')
  }
  const routing = setup({} as MockSink, devices)
  await routing.router.initialize()
  await routing.router.choose()
  expect(routing.state.support).toBe('unavailable')
  expect(choices).toBe(0)
})

test('applied state changes only after the browser reports a completed switch', async () => {
  const routing = setup()
  await routing.router.initialize()
  const pending = Promise.withResolvers<void>()
  const started = Promise.withResolvers<void>()
  routing.probe.setSinkId = async id => {
    started.resolve()
    await pending.promise
    routing.probe.sinkId = id
  }
  const selection = routing.router.select('bluetooth')
  await started.promise
  expect(routing.state.applied).toBe('')
  expect(routing.state.busy).toBe(true)
  pending.resolve()
  await selection
  expect(routing.state.applied).toBe('bluetooth')
  expect(routing.state.busy).toBe(false)
})

test('a resolved but ineffective setSinkId does not display the requested device', async () => {
  const routing = setup()
  await routing.router.initialize()
  routing.probe.setSinkId = async () => {}
  await routing.router.select('bluetooth')
  expect(routing.state.applied).toBe('')
  expect(routing.state.message).toContain('did not confirm')
})

test('a partially rejected multi-track switch restores the last applied route', async () => {
  const routing = setup()
  const track = sink()
  await routing.router.initialize()
  await routing.router.attach(track)
  await routing.router.select('bluetooth')
  track.setSinkId = async id => {
    if (id === 'speaker') throw new DOMException('Denied', 'NotAllowedError')
    track.sinkId = id
  }
  await routing.router.select('speaker')
  expect(routing.state.applied).toBe('bluetooth')
  expect(routing.probe.sinkId).toBe('bluetooth')
  expect(track.sinkId).toBe('bluetooth')
  expect(routing.state.message).toContain('permission was denied or blocked')
})

test('failed restoration reports mixed output as unknown instead of a selected wish', async () => {
  const routing = setup()
  const track = sink()
  await routing.router.initialize()
  await routing.router.attach(track)
  routing.probe.setSinkId = async id => {
    if (!id) throw new Error('Cannot restore default')
    routing.probe.sinkId = id
  }
  track.setSinkId = async id => {
    if (id) throw new Error('Device unavailable')
    track.sinkId = id
  }
  await routing.router.select('bluetooth')
  expect(routing.state.applied).toBeNull()
  expect(routing.state.message).toContain('Device unavailable')
})

test('the output picker runs in the user gesture and applies its returned device id', async () => {
  const devices = media()
  const pending = Promise.withResolvers<MediaDeviceInfo>()
  let choices = 0
  devices.selectAudioOutput = () => {
    choices++
    return pending.promise
  }
  const routing = setup(sink(), devices)
  await routing.router.initialize()
  expect(choices).toBe(0)
  expect(routing.state.chooser).toBe(true)
  const selection = routing.router.choose()
  expect(choices).toBe(1)
  expect(routing.state.applied).toBe('')
  pending.resolve(device('rotated-id', 'Granted headset'))
  await selection
  expect(routing.state.applied).toBe('rotated-id')
  expect(routing.state.devices).toContainEqual({ deviceId: 'rotated-id', label: 'Granted headset' })
  expect(devices.captures).toBe(0)
})

test('picker denial leaves the applied route unchanged and explains OS control', async () => {
  const devices = media()
  devices.selectAudioOutput = async () => {
    throw new DOMException('Dismissed', 'NotAllowedError')
  }
  const routing = setup(sink(), devices)
  await routing.router.initialize()
  await routing.router.select('speaker')
  await routing.router.choose()
  expect(routing.state.applied).toBe('speaker')
  expect(routing.state.message).toContain('permission was denied or blocked')
  expect(routing.state.message).toContain('phone or operating system')
})

test('new attachments inherit the applied output without touching microphone capture', async () => {
  const devices = media()
  const routing = setup(sink(), devices)
  await routing.router.initialize()
  await routing.router.select('bluetooth')
  const first = sink()
  const second = sink()
  await routing.router.attach(first)
  await routing.router.attach(second)
  expect(first.sinkId).toBe('bluetooth')
  expect(second.sinkId).toBe('bluetooth')
  expect(devices.captures).toBe(0)
})

test('devicechange/Bluetooth loss moves all playback elements to system default', async () => {
  const devices = media()
  const routing = setup(sink(), devices)
  const track = sink()
  await routing.router.initialize()
  await routing.router.attach(track)
  await routing.router.select('bluetooth')
  devices.outputs = [device('default')]
  devices.change()
  await routing.router.attach(sink()) // barrier after the queued devicechange
  expect(routing.state.applied).toBe('')
  expect(routing.probe.sinkId).toBe('')
  expect(track.sinkId).toBe('')
  expect(routing.states.some(state => state.message.includes('no longer available'))).toBe(true)
})

test('a failed default fallback never claims that Bluetooth loss was recovered', async () => {
  const devices = media()
  const routing = setup(sink(), devices)
  await routing.router.initialize()
  await routing.router.select('bluetooth')
  routing.probe.setSinkId = async id => {
    if (!id) throw new Error('Default route failed')
    routing.probe.sinkId = id
  }
  devices.outputs = [device('default')]
  await routing.router.refresh()
  expect(routing.state.applied).toBe('bluetooth')
  expect(routing.state.message).toContain('Default route failed')
  expect(routing.state.message).not.toContain('switched to system default')
})

test('device loss queued during a pending selection wins over its late completion', async () => {
  const devices = media()
  const routing = setup(sink(), devices)
  await routing.router.initialize()
  const pending = Promise.withResolvers<void>()
  const started = Promise.withResolvers<void>()
  routing.probe.setSinkId = async id => {
    if (id === 'bluetooth') {
      started.resolve()
      await pending.promise
    }
    routing.probe.sinkId = id
  }
  const selection = routing.router.select('bluetooth')
  await started.promise
  devices.outputs = [device('default')]
  devices.change()
  pending.resolve()
  await selection
  await routing.router.refresh()
  expect(routing.state.applied).toBe('')
  expect(routing.probe.sinkId).toBe('')
})

test('rapid selections and an attachment cannot finish in stale requested order', async () => {
  const routing = setup()
  await routing.router.initialize()
  const pending = Promise.withResolvers<void>()
  const started = Promise.withResolvers<void>()
  routing.probe.setSinkId = async id => {
    if (id === 'bluetooth') {
      started.resolve()
      await pending.promise
    }
    routing.probe.sinkId = id
  }
  const first = routing.router.select('bluetooth')
  await started.promise
  const track = sink()
  const attachment = routing.router.attach(track)
  const second = routing.router.select('speaker')
  pending.resolve()
  await Promise.all([first, attachment, second])
  expect(routing.state.applied).toBe('speaker')
  expect(routing.probe.sinkId).toBe('speaker')
  expect(track.sinkId).toBe('speaker')
})

test('reconnect rechecks availability and reapplies the browser output to existing tracks', async () => {
  const routing = setup()
  const track = sink()
  await routing.router.initialize()
  await routing.router.attach(track)
  await routing.router.select('bluetooth')
  track.sinkId = '' // the playback element was reset during reconnection
  await routing.router.refresh()
  expect(routing.state.applied).toBe('bluetooth')
  expect(track.sinkId).toBe('bluetooth')
})

test('standby clears old tracks; a new room/resume inherits the confirmed output', async () => {
  const routing = setup()
  const oldTrack = sink()
  await routing.router.initialize()
  await routing.router.attach(oldTrack)
  await routing.router.select('bluetooth')
  routing.router.clearTracks()
  const resumedTrack = sink()
  await routing.router.attach(resumedTrack)
  await routing.router.select('speaker')
  expect(resumedTrack.sinkId).toBe('speaker')
  expect(oldTrack.sinkId).toBe('bluetooth')
  expect(routing.state.applied).toBe('speaker')
})

test('an unsubscribed attachment pending behind a switch is never reattached', async () => {
  const routing = setup()
  await routing.router.initialize()
  const pending = Promise.withResolvers<void>()
  const started = Promise.withResolvers<void>()
  routing.probe.setSinkId = async id => {
    started.resolve()
    await pending.promise
    routing.probe.sinkId = id
  }
  const selection = routing.router.select('bluetooth')
  await started.promise
  const track = sink()
  const attachment = routing.router.attach(track)
  routing.router.detach(track)
  pending.resolve()
  await Promise.all([selection, attachment])
  expect(track.calls).toEqual([])
  expect(routing.state.applied).toBe('bluetooth')
})

test('enumeration denial reports unknown availability and actual mixed output on reconnect', async () => {
  const devices = media()
  const routing = setup(sink(), devices)
  const track = sink()
  await routing.router.initialize()
  await routing.router.attach(track)
  await routing.router.select('bluetooth')
  track.sinkId = ''
  devices.enumerateDevices = async () => {
    throw new DOMException('Blocked', 'NotAllowedError')
  }
  await routing.router.refresh()
  expect(routing.state.applied).toBeNull()
  expect(routing.state.message).toContain('device availability is unknown')
})

test('teardown removes listeners and prevents an outstanding selection from publishing', async () => {
  const devices = media()
  const routing = setup(sink(), devices)
  await routing.router.initialize()
  const pending = Promise.withResolvers<void>()
  const started = Promise.withResolvers<void>()
  routing.probe.setSinkId = async id => {
    started.resolve()
    await pending.promise
    routing.probe.sinkId = id
  }
  const selection = routing.router.select('bluetooth')
  await started.promise
  routing.router.dispose()
  const publications = routing.states.length
  const enumerations = devices.enumerations
  expect(devices.listeners.size).toBe(0)
  devices.change()
  pending.resolve()
  await selection
  await routing.router.attach(sink())
  expect(routing.states.length).toBe(publications)
  expect(devices.enumerations).toBe(enumerations)
})

test('a fresh panel starts at system default and does not reuse a prior room selection', async () => {
  const previous = setup()
  await previous.router.initialize()
  await previous.router.select('bluetooth')
  previous.router.dispose()
  const current = setup()
  await current.router.initialize()
  expect(current.state.applied).toBe('')
  expect(current.state.devices.find(item => item.deviceId === 'bluetooth')).toBeTruthy()
})
