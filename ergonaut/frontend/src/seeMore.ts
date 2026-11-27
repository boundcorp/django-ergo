// Older-page loading for a long chat. The transcript (`.chat-transcript`) is the scroll root on
// phone and desktop; the page shell does not scroll. See more stays a button: scrolling it into
// that root runs the same load, and a click remains the accessible fallback.

export const AT_BOTTOM_PX = 80

const MESSAGE_ID = /^m-\d+$/

export type ScrollAnchor = {
  /** `scrollHeight - scrollTop` captured before older messages were prepended. */
  fromBottom: number
  messageId: string | null
  /** Message top relative to the scroll root's top, in viewport pixels. */
  offset: number
}

export type OlderLoadTrigger = 'auto' | 'manual'

export type OlderLoadGate = {
  hasMore: boolean
  inFlight: boolean
  /** Auto loads stop after an error until the reader presses See more. */
  failed: boolean
  /** False until this chat's transcript has been placed (initial pin, or a deep link). */
  placed: boolean
  /** Reader is following the latest messages. Auto-load waits until they scroll up. */
  atBottom: boolean
  visible: boolean
  trigger: OlderLoadTrigger
}

export function isScrollableOverflow(value: string): boolean {
  return value === 'auto' || value === 'scroll' || value === 'overlay'
}

/** Nearest element, including `start`, whose overflow scrolls. Falls back to `start` when computed
 * styles are unavailable (tests, or a class that has not been applied yet). */
export function historyScrollRoot(start: HTMLElement): HTMLElement {
  const view = start.ownerDocument.defaultView
  if (!view) return start
  let node: HTMLElement | null = start
  while (node) {
    const style = view.getComputedStyle(node)
    if (isScrollableOverflow(style.overflowY) || isScrollableOverflow(style.overflow)) return node
    node = node.parentElement
  }
  return start
}

export function nearBottom(el: HTMLElement, threshold = AT_BOTTOM_PX): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight < threshold
}

export function elementVisibleInRoot(root: HTMLElement, node: HTMLElement): boolean {
  const view = root.getBoundingClientRect()
  const box = node.getBoundingClientRect()
  if (view.height === 0 || box.height === 0) return false
  return box.bottom > view.top + 1 && box.top < view.bottom - 1 && box.right > view.left && box.left < view.right
}

export function captureScrollAnchor(el: HTMLElement): ScrollAnchor {
  const view = el.getBoundingClientRect()
  const message = [...el.querySelectorAll<HTMLElement>('[id]')].find(node => {
    if (!MESSAGE_ID.test(node.id)) return false
    const box = node.getBoundingClientRect()
    return box.height > 0 && box.bottom > view.top + 1 && box.top < view.bottom
  })
  return {
    fromBottom: el.scrollHeight - el.scrollTop,
    messageId: message?.id ?? null,
    offset: message ? message.getBoundingClientRect().top - view.top : 0,
  }
}

export function restoreScrollAnchor(el: HTMLElement, anchor: ScrollAnchor): void {
  if (anchor.messageId && MESSAGE_ID.test(anchor.messageId)) {
    const node = el.ownerDocument.getElementById(anchor.messageId)
    if (node && el.contains(node)) {
      const delta = node.getBoundingClientRect().top - el.getBoundingClientRect().top - anchor.offset
      if (delta !== 0 || anchor.offset !== 0) {
        el.scrollTop += delta
        return
      }
    }
  }
  el.scrollTop = el.scrollHeight - anchor.fromBottom
}

/** Whether an older page may start. Manual clicks ignore visibility, placement and the error latch. */
export function shouldLoadOlder(gate: OlderLoadGate): boolean {
  if (!gate.hasMore || gate.inFlight) return false
  if (gate.trigger === 'manual') return true
  return gate.placed && gate.visible && !gate.atBottom && !gate.failed
}
