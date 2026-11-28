import { afterEach, beforeEach, expect, test } from 'bun:test'
import { act, createElement } from 'react'
import type { Root } from 'react-dom/client'
import { Window } from 'happy-dom'
import { MemoryRouter } from 'react-router-dom'
import type { Message } from '../api'
import { Transcript } from './Transcript'

// Reply updates (django_ergo.conversation.reply_updates): only explicit, server-accepted
// revisions change a reply; separate replies always stay separate.

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

const use = (line: number, id: string, name: string, input: object): Message => ({
  line,
  role: 'assistant',
  timestamp: null,
  blocks: [{ type: 'tool_use', id, name, input }],
})
const reply = (line: number, id: string, text: string, extra = {}) => use(line, id, 'send_reply', { text, ...extra })
const append = (line: number, id: string, text: string) =>
  use(line, id, 'ergo_interim_reply', { action: 'append', text })
const update = (line: number, id: string, target: string, revision: number, text: string, extra = {}) =>
  use(line, id, 'ergo_interim_reply', { action: 'update', reply_id: target, revision, text, ...extra })
const result = (line: number, id: string, content: unknown = 'ok', is_error = false): Message => ({
  line,
  role: 'user',
  timestamp: null,
  blocks: [{ type: 'tool_result', tool_use_id: id, content, is_error }],
})
const receipt = (line: number, id: string, reply_id: string, revision: number) =>
  result(line, id, JSON.stringify({ reply_id, revision, action: 'update' }))
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
const bubbles = () => [...container.querySelectorAll('[data-reply-id]')]
const text = (node: Element) => node.textContent ?? ''

test('separate replies are never merged or hidden', () => {
  render([
    reply(1, 'r1', 'First'),
    result(2, 'r1'),
    reply(3, 'r2', 'Second'),
    result(4, 'r2'),
    append(5, 'i1', 'Third'),
    result(6, 'i1', '{"reply_id":"i1","revision":0,"action":"append"}'),
  ])
  expect(bubbles().map(b => b.getAttribute('data-reply-id'))).toEqual(['r1', 'r2', 'i1'])
  expect(container.querySelector('.reply-revisions')).toBeNull()
})

test('an accepted update revises the reply in place and keeps its DOM node', () => {
  const history = [append(1, 'i1', 'Starting'), result(2, 'i1', '{"reply_id":"i1","revision":0}')]
  render(history)
  const node = container.querySelector('[data-reply-id="i1"]')
  // Still running: nothing changes yet.
  const running = [...history, update(3, 'u1', 'i1', 0, 'Half done')]
  render(running)
  expect(text(container.querySelector('[data-reply-id="i1"]')!)).toContain('Starting')
  const live = [...running, receipt(4, 'u1', 'i1', 1)]
  render(live)
  const same = container.querySelector('[data-reply-id="i1"]')!
  expect(same).toBe(node)
  expect(bubbles()).toHaveLength(1)
  const details = same.querySelector('.reply-revisions')!
  expect(details.querySelector('summary')!.textContent).toContain('1 earlier version')
  expect(text(details)).toContain('Starting')
  // The visible text is the latest version, outside the earlier-versions list.
  expect(text(same).replace(text(details), '')).toContain('Half done')

  // A reload gives the same picture from the same messages.
  act(() => root.unmount())
  const { createRoot } = require('react-dom/client')
  root = createRoot(container)
  render(live)
  expect(bubbles()).toHaveLength(1)
  expect(text(bubbles()[0]).replace(text(bubbles()[0].querySelector('.reply-revisions')!), '')).toContain('Half done')
})

test('a refused update changes nothing', () => {
  render([
    append(1, 'i1', 'Starting'),
    result(2, 'i1', '{}'),
    update(3, 'u1', 'i1', 4, 'Wrong'),
    result(4, 'u1', '{"error":"Stale revision","code":"stale"}', true),
  ])
  expect(bubbles()).toHaveLength(1)
  expect(text(bubbles()[0])).toContain('Starting')
  expect(container.textContent).not.toContain('Wrong')
  expect(container.querySelector('.reply-revisions')).toBeNull()
})

test('a final send_reply update lands in the reply it revises', () => {
  render([
    reply(1, 'r1', 'Tests running'),
    result(2, 'r1'),
    update(3, 'u1', 'r1', 0, 'Tests half done'),
    receipt(4, 'u1', 'r1', 1),
    reply(5, 'f1', 'Tests passed', { update_reply_id: 'r1', update_revision: 1, attachments: [] }),
    result(6, 'f1'),
  ])
  expect(bubbles()).toHaveLength(1)
  const only = bubbles()[0]
  expect(only.getAttribute('data-reply-id')).toBe('r1')
  expect(only.querySelector('summary')!.textContent).toContain('2 earlier versions')
  expect(text(only).replace(text(only.querySelector('.reply-revisions')!), '')).toContain('Tests passed')
})

test('questions and human messages stay visible around updates', () => {
  render([
    reply(1, 'r1', 'Working'),
    result(2, 'r1'),
    reply(3, 'q1', 'Tacos or soup?', { type: 'question', suggestions: ['Tacos', 'Soup'] }),
    result(4, 'q1'),
    human(5),
    reply(6, 'r2', 'Tacos then'),
    result(7, 'r2'),
  ])
  expect(bubbles().map(b => b.getAttribute('data-reply-id'))).toEqual(['r1', 'q1', 'r2'])
  expect(container.textContent).toContain('Tacos or soup?')
  expect(container.textContent).toContain('Next')
})

test('earlier versions keep their attachments', () => {
  render([
    use(1, 'i1', 'ergo_interim_reply', { action: 'append', text: 'Draft chart', attachments: ['draft.png'] }),
    result(2, 'i1', '{}'),
    update(3, 'u1', 'i1', 0, 'Final chart', { attachments: ['final.png'] }),
    receipt(4, 'u1', 'i1', 1),
  ])
  const details = container.querySelector('.reply-revisions')!
  expect(text(details)).toContain('Draft chart')
  expect(text(details)).toContain('draft.png')
  expect(text(bubbles()[0]).replace(text(details), '')).toContain('final.png')
})

test('loading older history keeps the reply node; an update whose reply is unloaded shows on its own until it loads', () => {
  const older = [reply(1, 'r1', 'Deploy started'), result(2, 'r1')]
  const newer = [
    update(3, 'u1', 'r1', 0, 'Deploy live'),
    receipt(4, 'u1', 'r1', 1),
    reply(5, 'r2', 'Anything else?'),
    result(6, 'r2'),
  ]
  render(newer)
  expect(container.textContent).toContain('Update to an earlier reply')
  const r2 = container.querySelector('[data-reply-id="r2"]')
  render([...older, ...newer])
  expect(container.textContent).not.toContain('Update to an earlier reply')
  expect(container.querySelector('[data-reply-id="r2"]')).toBe(r2)
  const r1 = container.querySelector('[data-reply-id="r1"]')!
  expect(text(r1)).toContain('Deploy live')
  // Paging further back again does not remount either reply.
  render([human(0), ...older, ...newer])
  expect(container.querySelector('[data-reply-id="r1"]')).toBe(r1)
  expect(container.querySelector('[data-reply-id="r2"]')).toBe(r2)
})

test('intervening tool calls keep their cards, results and open state through updates and reload', () => {
  const initial = [append(1, 'i1', 'Starting'), result(2, 'i1', '{"reply_id":"i1","revision":0}')]
  const tools = [
    use(3, 't1', 'ergo_bash', { command: 'printf tool-one' }),
    result(4, 't1', 'tool-one-result'),
    append(5, 'i2', 'Separate note'),
    result(6, 'i2', '{"reply_id":"i2","revision":0}'),
    use(7, 't2', 'ergo_lookup', { query: 'tool-two' }),
    result(8, 't2', 'tool-two-result'),
  ]
  render([...initial, ...tools])
  const node = bubbles()[0]
  const toolNode = container.querySelector('#m-3')!
  const toggle = toolNode.querySelector('button[aria-expanded]') as HTMLButtonElement
  act(() => toggle.click())
  const changed = [
    update(9, 'u1', 'i1', 0, 'Half done'),
    receipt(10, 'u1', 'i1', 1),
    reply(11, 'f1', 'All done', { update_reply_id: 'i1', update_revision: 1 }),
    result(12, 'f1'),
  ]
  const history = [...initial, ...tools, ...changed]
  render(history)
  expect(bubbles()[0]).toBe(node)
  expect(container.querySelector('#m-3')).toBe(toolNode)
  expect(toggle.getAttribute('aria-expanded')).toBe('true')
  const check = () => {
    expect(bubbles().map(b => b.getAttribute('data-reply-id'))).toEqual(['i1', 'i2'])
    expect(container.querySelectorAll('#m-3')).toHaveLength(1)
    expect(container.querySelectorAll('#m-7')).toHaveLength(1)
    for (const id of ['m-3', 'm-7']) {
      const button = container.querySelector(`#${id} button[aria-expanded]`) as HTMLButtonElement
      if (button.getAttribute('aria-expanded') === 'false') act(() => button.click())
    }
    expect(container.textContent).toContain('tool-one-result')
    expect(container.textContent).toContain('tool-two-result')
    expect(text(bubbles()[0])).toContain('All done')
    expect(text(bubbles()[0].querySelector('.reply-revisions')!)).toContain('Starting')
    expect(text(bubbles()[0].querySelector('.reply-revisions')!)).toContain('Half done')
    expect(text(bubbles()[1])).toContain('Separate note')
  }
  check()
  act(() => root.unmount())
  const { createRoot } = require('react-dom/client')
  root = createRoot(container)
  render(history)
  check()
})
