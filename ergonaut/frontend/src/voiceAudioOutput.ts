type SinkElement = HTMLMediaElement & {
  readonly sinkId?: string
  setSinkId?: (deviceId: string) => Promise<void>
}
type OutputDevices = MediaDevices & { selectAudioOutput?: () => Promise<MediaDeviceInfo> }
export type VoiceAudioOutputState = {
  support: 'checking' | 'available' | 'unavailable'
  applied: string | null
  devices: { deviceId: string; label: string }[]
  chooser: boolean
  busy: boolean
  message: string
}

export const phoneOutputGuidance =
  'Audio output is controlled by your phone or operating system. Use its audio route controls to choose Bluetooth or a speaker; the browser cannot confirm the physical route.'

export const initialVoiceAudioOutput: VoiceAudioOutputState = {
  support: 'checking',
  applied: null,
  devices: [],
  chooser: false,
  busy: false,
  message: 'Checking browser audio output controls…',
}

function failure(error: unknown) {
  if (error instanceof Error && error.name === 'NotAllowedError')
    return 'Audio output permission was denied or blocked by the browser.'
  return error instanceof Error ? error.message : String(error)
}

/** Routes only HTML media elements, matching Room's webAudioMix: false default.
 * The silent probe never plays media or requests capture. sinkId confirms only
 * the browser's selection, not which physical speaker is audible.
 */
export class VoiceAudioOutput {
  private elements = new Set<SinkElement>()
  private attachments = new Map<HTMLMediaElement, symbol>()
  private devices: OutputDevices | undefined
  private state = { ...initialVoiceAudioOutput }
  private active = true
  private pending = 0
  private queue = Promise.resolve()
  private changed = () => void this.refresh()

  constructor(
    private probe: SinkElement,
    mediaDevices: MediaDevices | undefined,
    private publish: (state: VoiceAudioOutputState) => void,
  ) {
    this.devices = mediaDevices
    this.elements.add(probe)
    this.devices?.addEventListener?.('devicechange', this.changed)
  }

  private update(change: Partial<VoiceAudioOutputState>) {
    if (!this.active) return
    this.state = { ...this.state, ...change }
    this.publish(this.state)
  }

  // Serialize selections, device loss and new attachments so a slow older
  // setSinkId cannot overwrite a later selection or a default fallback.
  private enqueue(work: () => Promise<void>) {
    if (!this.active) return Promise.resolve()
    this.pending++
    this.update({ busy: true })
    this.queue = this.queue.then(async () => {
      try {
        if (this.active) await work()
      } catch (error) {
        this.update({ message: `${failure(error)} ${phoneOutputGuidance}` })
      } finally {
        this.pending--
        this.update({ busy: this.pending > 0 })
      }
    })
    return this.queue
  }

  private readApplied() {
    const ids = [...this.elements].map(element => element.sinkId)
    const first = ids[0]
    return typeof first === 'string' && ids.every(id => id === first) ? first : null
  }

  private async setElements(deviceId: string) {
    const results = await Promise.allSettled(
      [...this.elements].map(async element => {
        if (!this.active || !this.elements.has(element)) return
        if (typeof element.setSinkId !== 'function') throw new Error('Audio output switching is unavailable.')
        await element.setSinkId(deviceId)
      }),
    )
    const rejected = results.find(result => result.status === 'rejected')
    if (rejected?.status === 'rejected') throw rejected.reason
  }

  private async apply(deviceId: string, message = '') {
    const previous = this.state.applied
    try {
      await this.setElements(deviceId)
      if (!this.active) return
      const applied = this.readApplied()
      this.update({
        applied,
        message:
          applied === deviceId
            ? message
            : 'The browser did not confirm the requested output on every audio element. Physical audio routing is unknown.',
      })
    } catch (error) {
      if (!this.active) return
      // A multi-track switch can partially succeed. Restore the last confirmed
      // route, then report the actual element state even if restoration fails.
      if (previous !== null) {
        try {
          await this.setElements(previous)
        } catch {
          // readApplied below must expose mixed/unknown state, not a wish.
        }
      }
      this.update({ applied: this.readApplied(), message: `${failure(error)} ${phoneOutputGuidance}` })
    }
  }

  initialize() {
    return this.enqueue(async () => {
      try {
        if (typeof this.probe.setSinkId !== 'function' || typeof this.probe.sinkId !== 'string')
          throw new Error('This browser does not expose working audio output controls.')
        await this.probe.setSinkId('')
        if (!this.active) return
        if (this.probe.sinkId !== '') throw new Error('The browser did not confirm system default output.')
        this.update({
          support: 'available',
          applied: '',
          chooser: typeof this.devices?.selectAudioOutput === 'function',
          message: '',
        })
        await this.refreshDevices()
      } catch (error) {
        this.update({ support: 'unavailable', applied: null, message: `${failure(error)} ${phoneOutputGuidance}` })
      }
    })
  }

  private async refreshDevices() {
    if (typeof this.devices?.enumerateDevices !== 'function') {
      await this.apply(this.state.applied ?? '')
      this.update({ message: 'The browser cannot list audio outputs. Only exposed browser controls are available.' })
      return
    }
    try {
      const devices = (await this.devices.enumerateDevices()).filter(device => device.kind === 'audiooutput')
      if (!this.active) return
      this.update({
        devices: devices
          .filter(device => device.deviceId && device.deviceId !== 'default')
          .map((device, index) => ({ deviceId: device.deviceId, label: device.label || `Audio output ${index + 1}` })),
      })
      const selected = this.state.applied
      if (selected && !devices.some(device => device.deviceId === selected)) {
        await this.apply('', 'The selected output is no longer available. Browser output switched to system default.')
      } else if (this.state.support === 'available') {
        await this.apply(selected ?? '')
      }
    } catch (error) {
      this.update({
        applied: this.readApplied(),
        message: `Could not refresh audio outputs; device availability is unknown. ${failure(error)}`,
      })
    }
  }

  refresh() {
    return this.enqueue(async () => {
      if (this.state.support === 'available') await this.refreshDevices()
    })
  }

  select(deviceId: string) {
    return this.enqueue(async () => {
      if (this.state.support === 'available') await this.apply(deviceId)
    })
  }

  choose() {
    if (!this.active || this.state.support !== 'available' || !this.devices?.selectAudioOutput) return Promise.resolve()
    // Invoke inside the click handler, before awaiting/queueing: the picker
    // requires transient user activation. Granting permission alone is not routing.
    let choice: Promise<MediaDeviceInfo>
    try {
      choice = this.devices.selectAudioOutput()
    } catch (error) {
      this.update({ message: `${failure(error)} ${phoneOutputGuidance}` })
      return Promise.resolve()
    }
    // Attach rejection handling immediately even if another operation is pending.
    const result = choice.then(
      device => ({ device }),
      error => ({ error }),
    )
    return this.enqueue(async () => {
      const selected = await result
      if (!this.active) return
      if ('error' in selected) {
        this.update({ message: `${failure(selected.error)} ${phoneOutputGuidance}` })
        return
      }
      const device = selected.device
      this.update({
        devices: [
          ...this.state.devices.filter(item => item.deviceId !== device.deviceId),
          { deviceId: device.deviceId, label: device.label || 'Selected audio output' },
        ],
      })
      await this.apply(device.deviceId)
    })
  }

  attach(element: HTMLMediaElement) {
    if (!this.active) return Promise.resolve()
    const attachment = Symbol()
    this.attachments.set(element, attachment)
    return this.enqueue(async () => {
      if (this.attachments.get(element) !== attachment) return
      this.elements.add(element)
      if (this.state.support === 'available') await this.apply(this.state.applied ?? '')
    })
  }

  detach(element: HTMLMediaElement) {
    this.elements.delete(element)
    this.attachments.delete(element)
  }

  clearTracks() {
    this.elements.clear()
    this.attachments.clear()
    this.elements.add(this.probe)
    if (this.state.support === 'available') this.update({ applied: this.readApplied() })
  }

  dispose() {
    this.active = false
    this.attachments.clear()
    this.devices?.removeEventListener?.('devicechange', this.changed)
    this.elements.clear()
  }
}
