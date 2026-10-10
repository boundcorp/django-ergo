export const QUICK_REPLY_HOLD_MS = 700
export const QUICK_REPLY_MOVE_TOLERANCE_PX = 10

export type QuickReplyTimer = {
  setTimeout: (fn: () => void, ms: number) => unknown
  clearTimeout: (handle: unknown) => void
}

type ActiveHold =
  | { kind: 'keyboard'; text: string }
  | { kind: 'pointer'; text: string; pointerId: number; x: number; y: number }

const realTimers: QuickReplyTimer = {
  setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimeout: handle => globalThis.clearTimeout(handle as number),
}

/**
 * Owns one chip row's deliberate-send gesture. Keeping it independent of React
 * makes timer cleanup and pointer/keyboard cancellation deterministic.
 */
export class QuickReplyHoldController {
  private active: ActiveHold | null = null
  private timer: unknown = null
  private disabled = false
  private fired = false

  constructor(
    private readonly onHoldingChange: (text: string | null) => void,
    private readonly onSend: (text: string) => void,
    private readonly timers: QuickReplyTimer = realTimers,
  ) {}

  setDisabled(disabled: boolean) {
    this.disabled = disabled
    if (disabled) this.cancel()
  }

  beginPointer(text: string, pointerId: number, x: number, y: number): boolean {
    if (this.disabled || this.fired || this.active) return false
    this.active = { kind: 'pointer', text, pointerId, x, y }
    this.begin()
    return true
  }

  movePointer(pointerId: number, x: number, y: number) {
    const active = this.active
    if (active?.kind !== 'pointer' || active.pointerId !== pointerId) return
    if (Math.hypot(x - active.x, y - active.y) > QUICK_REPLY_MOVE_TOLERANCE_PX) this.cancel()
  }

  cancelPointer(pointerId: number) {
    if (this.active?.kind === 'pointer' && this.active.pointerId === pointerId) this.cancel()
  }

  beginKeyboard(text: string): boolean {
    if (this.disabled || this.fired || this.active) return false
    this.active = { kind: 'keyboard', text }
    this.begin()
    return true
  }

  endKeyboard() {
    if (this.active?.kind === 'keyboard') this.cancel()
  }

  cancel() {
    if (this.timer != null) this.timers.clearTimeout(this.timer)
    this.timer = null
    if (!this.active) return
    this.active = null
    this.onHoldingChange(null)
  }

  /** Re-enable after the enclosing chat has finished the send. */
  reset() {
    this.cancel()
    this.fired = false
  }

  dispose() {
    this.cancel()
  }

  private begin() {
    this.onHoldingChange(this.active!.text)
    this.timer = this.timers.setTimeout(() => this.fire(), QUICK_REPLY_HOLD_MS)
  }

  private fire() {
    this.timer = null
    const active = this.active
    if (!active || this.disabled || this.fired) return
    this.active = null
    this.fired = true
    this.onHoldingChange(null)
    this.onSend(active.text)
  }
}
