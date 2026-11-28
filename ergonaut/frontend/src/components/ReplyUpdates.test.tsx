import { afterEach, beforeEach, expect, test } from 'bun:test'
import { act, createElement } from 'react'
import type { Root } from 'react-dom/client'
import { Window } from 'happy-dom'
import { MemoryRouter } from 'react-router-dom'
import type { Message } from '../api'
import { Transcript } from './Transcript'

let window: Window
let root: Root
let container: HTMLDivElement
beforeEach(async () => {
  window = new Window({ url: 'http://localhost' })
  Object.assign(globalThis, {
    window,
    document: window.document,
    navigator: window.navigator,
    HTMLElement: window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true,
  })
  const { createRoot } = await import('react-dom/client')
  container = window.document.createElement('div')
  window.document.body.append(container)
  root = createRoot(container)
})
afterEach(() => {
  act(() => root.unmount())
  window.happyDOM.close()
})
const reply = (line: number, text: string, extra = {}): Message => ({
  line,
  role: 'assistant',
  timestamp: null,
  blocks: [{ type: 'tool_use', id: `reply${line}`, name: 'send_reply', input: { text, ...extra } }],
})
const result = (line: number, id: string): Message => ({
  line,
  role: 'user',
  timestamp: null,
  blocks: [{ type: 'tool_result', tool_use_id: id, content: 'ok' }],
})
const human = (line: number): Message => ({
  line,
  role: 'user',
  timestamp: null,
  blocks: [{ type: 'text', text: 'Next' }],
})
function render(messages: Message[]) {
  act(() =>
    root.render(createElement(MemoryRouter, null, createElement(Transcript, { messages, calls: [], sessionId: 's' }))),
  )
}

test('live updates retain the same container and reload produces the same grouping', () => {
  const first = reply(1, 'First')
  const history = [first, result(2, 'reply1'), reply(3, 'Second')]
  render([first])
  const node = container.querySelector('[data-reply-updates]')
  render(history)
  expect(container.querySelector('[data-reply-updates]')).toBe(node)
  expect(container.querySelectorAll('[data-reply-updates]').length).toBe(1)
  expect(container.textContent).toContain('1 earlier update')
  expect(container.querySelector('details')?.textContent).toContain('First')
  expect(container.textContent).toContain('Second')
  const live = container.innerHTML
  render(history)
  expect(container.innerHTML).toBe(live)
})

test('human messages, authors and questions break groups; earlier attachments remain accessible', () => {
  const attached = reply(1, 'File', { attachments: ['report.pdf'] })
  const other = { ...reply(8, 'Another author'), author: { kind: 'bot' as const, ref: 'other', display_name: 'Other' } }
  render([
    attached,
    result(2, 'reply1'),
    reply(3, 'Progress'),
    human(4),
    reply(5, 'Question?', { type: 'question' }),
    result(6, 'reply5'),
    reply(7, 'After question'),
    other,
  ])
  expect(container.querySelectorAll('[data-reply-updates]').length).toBe(3)
  expect(container.querySelector('details')?.textContent).toContain('File')
  expect(container.querySelector('details')?.textContent).toContain('report.pdf')
  expect(container.textContent).toContain('Question?')
  expect(container.textContent).toContain('After question')
  expect(container.textContent).toContain('Another author')
})
