// The viewer side of a page's bridge: the postMessage protocol between a sandboxed page iframe
// and PageViewer (docs/specs/page-actions.md). DOM-free so it can be tested without a browser;
// Pins.tsx wires it to the iframe, the dialogs and the live stream.
import type { PageActionBody, PageActionResponse } from './api'

export { tableEventsUrl as eventsUrl } from './api'

/** A change settles for this long before the page re-renders. */
export const RERENDER_DELAY_MS = 1000
/** How long the viewer waits for the page to report its scroll position before reloading anyway. */
export const SCROLL_TIMEOUT_MS = 300

export type ToastKind = 'info' | 'error'

/** Messages the viewer sends to the page. */
export type ViewerMessage =
  | { type: 'ergo:result'; id: string; ok: true; result: Record<string, unknown> }
  | { type: 'ergo:result'; id: string; ok: false; error: string }
  | { type: 'ergo:changed'; table: string }
  | { type: 'ergo:getscroll' }
  | { type: 'ergo:restore'; y: number }

/** Timer functions, injectable so the debounce can be tested without waiting. */
export type Timers = {
  setTimeout: (fn: () => void, ms: number) => unknown
  clearTimeout: (handle: unknown) => void
}

const realTimers: Timers = {
  setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimeout: handle => globalThis.clearTimeout(handle as number),
}

export type BridgeDeps = {
  /** The bot, page and session come from the viewer's pin and session, never from the page. */
  bot: string
  page: string
  sessionId: string | null
  /** The iframe's current contentWindow (null before it exists). */
  frameWindow: () => unknown
  postToPage: (message: ViewerMessage) => void
  callAction: (bot: string, name: string, body: PageActionBody) => Promise<PageActionResponse>
  /** Ask the user to approve an action; resolves true for Yes. */
  confirm: (preview: string) => Promise<boolean>
  notify: (text: string, kind: ToastKind, href?: string) => void
  openUrl: (url: string) => void
  /** Re-render the page (the iframe is reloaded; the new page posts ergo:ready). */
  reloadFrame: () => void
  /** The tables the live stream should cover changed (empty = no stream). */
  onStreamTables?: (tables: string[]) => void
  timers?: Timers
}

/** A message from the page: its `type` starts `ergo:`; the other fields are checked by its handler. */
export type PageMessage = { type: string } & Record<string, unknown>

type Handler = (message: PageMessage) => void | Promise<void>

/** The page message in `data`, or null when it isn't an object with a string `type` starting `ergo:`. */
export function ergoMessage(data: unknown): PageMessage | null {
  if (typeof data !== 'object' || data === null || Array.isArray(data)) return null
  if (!('type' in data) || typeof data.type !== 'string' || !data.type.startsWith('ergo:')) return null
  return { ...data, type: data.type }
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : []
}

/** Only http(s) URLs may be opened from an action result. */
export function safeOpenUrl(value: unknown): string | null {
  if (typeof value !== 'string') return null
  try {
    const url = new URL(value)
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null
  } catch {
    return null
  }
}

/** The `changed` list of a stream event's data, or null when it isn't one. */
export function parseStreamEvent(raw: unknown): string[] | null {
  if (typeof raw !== 'string') return null
  try {
    const data: unknown = JSON.parse(raw)
    if (typeof data !== 'object' || data === null || !('changed' in data) || !Array.isArray(data.changed)) return null
    return strings(data.changed)
  } catch {
    return null
  }
}

export class PageBridge {
  private deps: BridgeDeps
  private timers: Timers
  private handlers: Record<string, Handler>

  private tables: string[] = []
  private listening: string[] = []
  private stream: string[] = []

  private focused = false
  private rerenderPending = false
  private rerenderTimer: unknown = null

  private waitingScroll = false
  private scrollTimer: unknown = null
  private restoreY: number | null = null

  constructor(deps: BridgeDeps) {
    this.deps = deps
    this.timers = deps.timers ?? realTimers
    // Part 3 adds its block messages here; the table is keyed by message type.
    this.handlers = {
      'ergo:call': m => this.handleCall(m),
      'ergo:ready': m => this.handleReady(m),
      'ergo:reload': () => this.reloadNow(),
      'ergo:focus': m => this.handleFocus(m),
      'ergo:scroll': m => this.handleScroll(m),
    }
  }

  /** Handle a window `message` event; false when it isn't from this page's iframe or isn't ours. */
  async handleMessage(source: unknown, data: unknown): Promise<boolean> {
    const frame = this.deps.frameWindow()
    if (frame == null || source !== frame) return false
    const message = ergoMessage(data)
    if (!message) return false
    const handler = this.handlers[message.type]
    if (!handler) return false
    await handler(message)
    return true
  }

  // --- calls ---------------------------------------------------------------------------

  private reply(id: string, outcome: { ok: true; result: Record<string, unknown> } | { ok: false; error: string }) {
    this.deps.postToPage({ type: 'ergo:result', id, ...outcome })
  }

  private async handleCall(message: PageMessage): Promise<void> {
    const { id, name, args } = message
    if (typeof id !== 'string') return
    if (typeof name !== 'string' || !name) return this.reply(id, { ok: false, error: 'Missing action name' })
    if (args != null && (typeof args !== 'object' || Array.isArray(args))) {
      return this.reply(id, { ok: false, error: 'Arguments must be an object' })
    }
    const { bot, page, sessionId } = this.deps
    const body: PageActionBody = { args: { ...args }, page, session_id: sessionId }
    try {
      let response = await this.deps.callAction(bot, name, body)
      if ('needs_approval' in response) {
        // The token stays here; the page only ever sees the outcome.
        if (!(await this.deps.confirm(response.preview))) return this.reply(id, { ok: false, error: 'Cancelled' })
        response = await this.deps.callAction(bot, name, { ...body, approval: response.approval })
        if ('needs_approval' in response) throw new Error('The approval was not accepted')
      }
      const result = response.result ?? {}
      this.reply(id, { ok: true, result })
      this.applyResult(result)
    } catch (e) {
      const error = e instanceof Error ? e.message : String(e)
      this.reply(id, { ok: false, error })
      this.deps.notify(error, 'error')
    }
  }

  private applyResult(result: Record<string, unknown>) {
    const sessionId = typeof result.session_id === 'string' ? result.session_id : ''
    const chat = typeof result.chat === 'string' ? result.chat : ''
    if (sessionId && chat) {
      this.deps.notify(`Sent to ${chat}`, 'info', `/s/${encodeURIComponent(sessionId)}`)
    } else if (typeof result.message === 'string' && result.message) {
      this.deps.notify(result.message, 'info')
    }
    const url = safeOpenUrl(result.open)
    if (url) this.deps.openUrl(url)
    if (result.reload === true) this.reloadNow()
  }

  // --- ready, focus, scroll ------------------------------------------------------------

  private handleReady(message: PageMessage) {
    this.tables = strings(message.tables)
    this.listening = strings(message.listening)
    // A table the page handles itself needs the stream even if it never read it at render time.
    const stream = [...new Set([...this.tables, ...this.listening])].sort()
    if (stream.length !== this.stream.length || stream.some((t, i) => t !== this.stream[i])) {
      this.stream = stream
      this.deps.onStreamTables?.(stream)
    }
    if (this.restoreY !== null) {
      const y = this.restoreY
      this.restoreY = null
      this.deps.postToPage({ type: 'ergo:restore', y })
    }
  }

  private handleFocus(message: PageMessage) {
    this.focused = message.focused === true
    // Typing has stopped: a deferred re-render runs a second after the field blurs.
    if (!this.focused && this.rerenderPending) this.arm()
  }

  private handleScroll(message: PageMessage) {
    if (!this.waitingScroll) return
    this.finishReload(typeof message.y === 'number' && Number.isFinite(message.y) ? message.y : null)
  }

  // --- changes and re-rendering ---------------------------------------------------------

  /** A stream event's data (JSON); the first event after connect is a baseline with nothing changed. */
  handleStreamData(raw: unknown) {
    const changed = parseStreamEvent(raw)
    if (changed) this.handleChanged(changed)
  }

  handleChanged(changed: string[]) {
    let rerender = false
    for (const table of changed) {
      if (this.listening.includes(table)) this.deps.postToPage({ type: 'ergo:changed', table })
      else rerender = true
    }
    if (rerender) {
      this.rerenderPending = true
      this.arm()
    }
  }

  private arm() {
    this.timers.clearTimeout(this.rerenderTimer)
    this.rerenderTimer = this.timers.setTimeout(() => {
      this.rerenderTimer = null
      // While a field has focus the re-render stays pending; blur re-arms it.
      if (this.rerenderPending && !this.focused) this.reloadNow()
    }, RERENDER_DELAY_MS)
  }

  /** Re-render now, keeping the scroll position: ask the page for it, then reload the iframe. */
  reloadNow() {
    this.cancelRerender()
    if (this.waitingScroll) return
    this.waitingScroll = true
    this.scrollTimer = this.timers.setTimeout(() => this.finishReload(null), SCROLL_TIMEOUT_MS)
    this.deps.postToPage({ type: 'ergo:getscroll' })
  }

  private finishReload(y: number | null) {
    this.timers.clearTimeout(this.scrollTimer)
    this.scrollTimer = null
    this.waitingScroll = false
    // A page that hasn't answered keeps the position an earlier reload is still waiting to restore.
    if (y !== null) this.restoreY = y
    this.focused = false // the document that held the focus is gone
    this.deps.reloadFrame()
  }

  private cancelRerender() {
    this.timers.clearTimeout(this.rerenderTimer)
    this.rerenderTimer = null
    this.rerenderPending = false
  }

  /** The iframe was reloaded from outside (Reload button, new chat content): no scroll to restore. */
  frameReplaced() {
    this.cancelRerender()
    this.timers.clearTimeout(this.scrollTimer)
    this.scrollTimer = null
    this.waitingScroll = false
    this.restoreY = null
    this.focused = false
  }

  dispose() {
    this.frameReplaced()
  }
}
