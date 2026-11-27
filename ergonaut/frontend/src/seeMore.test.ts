import { describe, expect, test } from 'bun:test'
import { Window } from 'happy-dom'
import {
  captureScrollAnchor,
  elementVisibleInRoot,
  historyScrollRoot,
  nearBottom,
  restoreScrollAnchor,
  shouldLoadOlder,
  type OlderLoadGate,
} from './seeMore'

function gate(overrides: Partial<OlderLoadGate> = {}): OlderLoadGate {
  return {
    hasMore: true,
    inFlight: false,
    failed: false,
    placed: true,
    atBottom: false,
    visible: true,
    trigger: 'auto',
    ...overrides,
  }
}

describe('shouldLoadOlder', () => {
  test('auto-loads only when the button is in view after the reader scrolls up', () => {
    expect(shouldLoadOlder(gate())).toBe(true)
    expect(shouldLoadOlder(gate({ visible: false }))).toBe(false)
    expect(shouldLoadOlder(gate({ atBottom: true }))).toBe(false)
    expect(shouldLoadOlder(gate({ placed: false }))).toBe(false)
    expect(shouldLoadOlder(gate({ hasMore: false }))).toBe(false)
    expect(shouldLoadOlder(gate({ inFlight: true }))).toBe(false)
    expect(shouldLoadOlder(gate({ failed: true }))).toBe(false)
  })

  test('a click still loads while at the bottom, before placement, or after an error', () => {
    expect(shouldLoadOlder(gate({ trigger: 'manual', atBottom: true, visible: false, placed: false }))).toBe(true)
    expect(shouldLoadOlder(gate({ trigger: 'manual', failed: true }))).toBe(true)
    expect(shouldLoadOlder(gate({ trigger: 'manual', inFlight: true }))).toBe(false)
    expect(shouldLoadOlder(gate({ trigger: 'manual', hasMore: false }))).toBe(false)
  })
})

describe('history scroll root', () => {
  const window = new Window()
  const document = window.document

  test('phone and desktop use the transcript when it scrolls, otherwise a scrollable ancestor', () => {
    const page = document.createElement('div')
    const transcript = document.createElement('div')
    transcript.className = 'chat-transcript'
    page.append(transcript)
    document.body.append(page)

    expect(historyScrollRoot(transcript)).toBe(transcript)

    page.style.overflowY = 'auto'
    transcript.style.overflowY = 'visible'
    expect(historyScrollRoot(transcript)).toBe(page)

    transcript.style.overflowY = 'auto'
    expect(historyScrollRoot(transcript)).toBe(transcript)
  })
})

describe('scroll anchor', () => {
  test('keeps the message that was in frame, and falls back to the distance from the bottom', () => {
    const window = new Window()
    const document = window.document
    const box = document.createElement('div')
    const message = document.createElement('div')
    message.id = 'm-12'
    box.append(message)
    document.body.append(box)
    Object.defineProperty(box, 'scrollHeight', { configurable: true, value: 1000 })
    Object.defineProperty(box, 'clientHeight', { configurable: true, value: 400 })
    box.scrollTop = 120
    box.getBoundingClientRect = () => rect(100, 500)
    message.getBoundingClientRect = () => rect(180, 220)

    const anchor = captureScrollAnchor(box)
    expect(anchor).toEqual({ fromBottom: 880, messageId: 'm-12', offset: 80 })
    expect(nearBottom(box)).toBe(false)
    expect(elementVisibleInRoot(box, message)).toBe(true)

    message.getBoundingClientRect = () => rect(420, 460)
    restoreScrollAnchor(box, anchor)
    expect(box.scrollTop).toBe(120 + (420 - 100 - 80))

    message.remove()
    Object.defineProperty(box, 'scrollHeight', { configurable: true, value: 1600 })
    restoreScrollAnchor(box, anchor)
    expect(box.scrollTop).toBe(1600 - 880)
  })
})

function rect(top: number, bottom: number): DOMRect {
  return {
    top,
    bottom,
    left: 0,
    right: 200,
    width: 200,
    height: bottom - top,
    x: 0,
    y: top,
    toJSON() {
      return {}
    },
  } as DOMRect
}
