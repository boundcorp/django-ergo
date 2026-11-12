import { expect, test } from 'bun:test'
import { Window } from 'happy-dom'
import { collectVoiceAudioDiagnostics } from './voiceAudioDiagnostics'
import { initialVoiceAudioOutput } from './voiceAudioOutput'

test('route observations exclude hardware identifiers and do not request capture or select output', () => {
  const previous = { window: globalThis.window, document: globalThis.document, navigator: globalThis.navigator }
  const window = new Window()
  Object.assign(globalThis, { window, document: window.document, navigator: window.navigator })
  try {
    let captures = 0
    Object.defineProperty(window.navigator, 'mediaDevices', {
      value: {
        getUserMedia: async () => {
          captures++
          throw new Error('Diagnostics must never capture')
        },
      },
    })
    const audio = window.document.createElement('audio')
    const track = {
      readyState: 'live',
      enabled: false,
      muted: true,
      getSettings: () => ({
        deviceId: 'PRIVATE-MIC-ID',
        groupId: 'PRIVATE-GROUP',
        label: 'PRIVATE-HEADSET',
        echoCancellation: true,
        autoGainControl: false,
        noiseSuppression: true,
      }),
    } as unknown as MediaStreamTrack
    const snapshot = collectVoiceAudioDiagnostics(
      {
        ...initialVoiceAudioOutput,
        support: 'available',
        applied: 'PRIVATE-SINK-ID',
        devices: [{ deviceId: 'PRIVATE-SINK-ID', label: 'PRIVATE-NAME' }],
      },
      [audio as unknown as HTMLMediaElement],
      track,
    )
    expect(snapshot.applied_sink).toBe('selected')
    expect(snapshot.physical_route).toBe('unknown')
    expect(snapshot.microphone_enabled).toBe(false)
    expect(snapshot.microphone_muted).toBe(true)
    expect(snapshot.echo_cancellation).toBe(true)
    expect(JSON.stringify(snapshot)).not.toContain('PRIVATE')
    expect(captures).toBe(0)
    expect(collectVoiceAudioDiagnostics(initialVoiceAudioOutput, []).microphone_state).toBe('unknown')
  } finally {
    Object.assign(globalThis, previous)
    window.happyDOM.abort()
  }
})
