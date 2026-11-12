import type { VoiceAudioOutputState } from './voiceAudioOutput'

export type VoiceAudioDiagnostics = {
  observed_at: string
  browser: string
  browser_major: string
  platform: string
  display_mode: string
  secure_context: boolean
  sink_api: boolean
  chooser_api: boolean
  output_support: VoiceAudioOutputState['support']
  applied_sink: 'unknown' | 'default' | 'selected'
  speaker_policy: string
  audio_session: string
  playback_path: 'html-media-elements'
  attached_elements: number
  playing_elements: number
  muted_elements: number
  microphone_state: string
  microphone_enabled: boolean | null
  microphone_muted: boolean | null
  echo_cancellation: boolean | null
  auto_gain_control: boolean | null
  noise_suppression: boolean | null
  physical_route: 'unknown'
}

/** Observations only: never request capture, select a sink, or infer a physical route. */
export function collectVoiceAudioDiagnostics(
  output: VoiceAudioOutputState,
  elements: HTMLMediaElement[],
  microphone?: MediaStreamTrack,
): VoiceAudioDiagnostics {
  const ua = navigator.userAgent
  const browser = /(?:Firefox|FxiOS)\/(\d+)/.exec(ua)
    ? 'firefox'
    : /(?:Chrome|CriOS)\/(\d+)/.test(ua)
      ? 'chromium'
      : /Version\/(\d+).*Safari/.test(ua)
        ? 'safari'
        : 'unknown'
  const version = /(?:Firefox|FxiOS|Chrome|CriOS|Version)\/(\d+)/.exec(ua)?.[1] ?? 'unknown'
  const settings = microphone?.getSettings()
  const session = (navigator as Navigator & { audioSession?: { type?: string } }).audioSession?.type
  const policy = (document as Document & { permissionsPolicy?: { allowsFeature(name: string): boolean } })
    .permissionsPolicy
  return {
    observed_at: new Date().toISOString(),
    browser,
    browser_major: version,
    platform: /Android/.test(ua) ? 'android' : /iPhone|iPad/.test(ua) ? 'ios' : 'other',
    display_mode: window.matchMedia('(display-mode: standalone)').matches ? 'standalone' : 'browser',
    secure_context: window.isSecureContext,
    sink_api: typeof document.createElement('audio').setSinkId === 'function',
    chooser_api:
      typeof (navigator.mediaDevices as (MediaDevices & { selectAudioOutput?: unknown }) | undefined)
        ?.selectAudioOutput === 'function',
    output_support: output.support,
    applied_sink: output.applied === null ? 'unknown' : output.applied === '' ? 'default' : 'selected',
    speaker_policy: policy ? (policy.allowsFeature('speaker-selection') ? 'allowed' : 'blocked') : 'unknown',
    audio_session: ['auto', 'playback', 'transient', 'transient-solo', 'ambient', 'play-and-record'].includes(
      session ?? '',
    )
      ? (session ?? 'unknown')
      : 'unknown',
    playback_path: 'html-media-elements',
    attached_elements: elements.length,
    playing_elements: elements.filter(element => !element.paused && !element.ended && element.readyState >= 2).length,
    muted_elements: elements.filter(element => element.muted || element.volume === 0).length,
    microphone_state: microphone?.readyState ?? 'unknown',
    microphone_enabled: microphone?.enabled ?? null,
    microphone_muted: microphone?.muted ?? null,
    echo_cancellation: settings?.echoCancellation ?? null,
    auto_gain_control: settings?.autoGainControl ?? null,
    noise_suppression: settings?.noiseSuppression ?? null,
    physical_route: 'unknown',
  }
}
