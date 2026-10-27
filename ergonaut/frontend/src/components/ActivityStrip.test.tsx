import { afterEach, beforeEach, describe, expect, test } from 'bun:test'
import { act, createElement } from 'react'
import type { createRoot as CreateRoot, Root } from 'react-dom/client'
import { Window } from 'happy-dom'
import type { DelegatedRequest, Todo, Worker } from '../api'
import { ActivityStrip } from './ActivityStrip'

const request = (status: DelegatedRequest['status']): DelegatedRequest => ({
  id: status,
  direction: 'out',
  other_session_id: null,
  other: 'Bot',
  text: 'Help',
  status,
  reply: '',
  created_at: '',
  updated_at: '',
})
const worker = (status: Worker['status']): Worker => ({
  id: status,
  title: 'Work',
  function: 'work',
  status,
  progress: '',
  result: null,
  error: '',
  created_at: '',
  completed_at: null,
})
const todos: Todo[] = [
  { content: 'Finished task', status: 'completed' },
  { content: 'Current task', status: 'in_progress' },
]

let window: Window
let root: Root
let createRoot: typeof CreateRoot
let container: HTMLDivElement
const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')

function render(requests: DelegatedRequest[] = [], workers: Worker[] = [], items: Todo[] = todos) {
  act(() =>
    root.render(
      createElement(ActivityStrip, { requests, workers, todos: items }, createElement('p', null, 'Activity details')),
    ),
  )
}

function disclosure() {
  const button = container.querySelector('button')
  if (!button) throw new Error('Missing activity button')
  return button
}

beforeEach(async () => {
  window = new Window({ url: 'http://localhost' })
  Object.assign(globalThis, {
    window,
    document: window.document,
    navigator: window.navigator,
    HTMLElement: window.HTMLElement,
    Event: window.Event,
    MouseEvent: window.MouseEvent,
    IS_REACT_ACT_ENVIRONMENT: true,
  })
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: window.localStorage })
  // React DOM captures the document at module initialization, so install Happy DOM first.
  createRoot = (await import('react-dom/client')).createRoot
  container = window.document.createElement('div')
  window.document.body.append(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => root.unmount())
  if (originalStorage) Object.defineProperty(globalThis, 'localStorage', originalStorage)
  else Reflect.deleteProperty(globalThis, 'localStorage')
  window.happyDOM.close()
})

describe('ActivityStrip', () => {
  test('summarizes live requests, workers and todos, and hides when there is no visible activity', () => {
    render(
      [request('waiting'), request('answered'), request('failed')],
      [worker('queued'), worker('running'), worker('completed')],
    )
    const button = disclosure()
    expect(button.textContent).toContain('1 request')
    expect(button.textContent).toContain('2 active / 3 workers')
    expect(button.textContent).toContain('1 of 2 done')
    expect(button.textContent).toContain('Current task')
    expect(container.querySelector('section')?.getAttribute('aria-label')).toBe('Chat activity')
    render([request('answered'), request('failed')], [], [])
    expect(container.textContent).toBe('')
  })

  test('discloses details through a native button and persists both choices across remounts', () => {
    render()
    let button = disclosure()
    expect(button.type).toBe('button')
    expect(button.getAttribute('aria-expanded')).toBe('false')
    const panel = window.document.getElementById(button.getAttribute('aria-controls')!)!
    expect(panel.hidden).toBe(true)
    expect(panel.textContent).not.toContain('Activity details')
    act(() => button.click())
    expect(button.getAttribute('aria-expanded')).toBe('true')
    expect(panel.hidden).toBe(false)
    expect(panel.textContent).toContain('Activity details')
    expect(window.localStorage.getItem('ergonaut.activity')).toBe('open')

    act(() => root.unmount())
    root = createRoot(container)
    render()
    button = disclosure()
    expect(button.getAttribute('aria-expanded')).toBe('true')
    expect(container.textContent).toContain('Activity details')
    act(() => button.click())
    expect(button.getAttribute('aria-expanded')).toBe('false')
    expect(window.localStorage.getItem('ergonaut.activity')).toBe('closed')
    act(() => root.unmount())
    root = createRoot(container)
    render()
    expect(disclosure().getAttribute('aria-expanded')).toBe('false')
  })

  test('still toggles when storage is unavailable', () => {
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      get() {
        throw new Error('Storage unavailable')
      },
    })
    render()
    const button = disclosure()
    expect(button.getAttribute('aria-expanded')).toBe('false')
    act(() => button.click())
    expect(button.getAttribute('aria-expanded')).toBe('true')
    expect(container.textContent).toContain('Activity details')
    act(() => button.click())
    expect(button.getAttribute('aria-expanded')).toBe('false')
  })
})
