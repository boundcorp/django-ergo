import { afterEach, beforeEach, describe, expect, test } from 'bun:test'
import { act, createElement } from 'react'
import type { createRoot as CreateRoot, Root } from 'react-dom/client'
import { Window } from 'happy-dom'
import { api, type DelegatedRequest, type Worker } from '../api'
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

let window: Window
let root: Root
let createRoot: typeof CreateRoot
let container: HTMLDivElement
const originalLog = api.workerLog
const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')

function render(requests: DelegatedRequest[] = [request('waiting')], workers: Worker[] = [], sessionId = 's1') {
  act(() =>
    root.render(
      createElement(ActivityStrip, { sessionId, requests, workers }, createElement('p', null, 'Activity details')),
    ),
  )
}

function disclosure() {
  const button = container.querySelector<HTMLButtonElement>('button[aria-controls]')
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
  api.workerLog = originalLog
  if (originalStorage) Object.defineProperty(globalThis, 'localStorage', originalStorage)
  else Reflect.deleteProperty(globalThis, 'localStorage')
  window.happyDOM.close()
})

const agent = (id: string, title: string, model: string, over: Partial<Worker> = {}): Worker => ({
  ...worker('running'),
  id,
  session_id: 's1',
  function: 'agent:orca',
  title: `claude: ${title}`,
  created_at: new Date(Date.now() - 5 * 60_000).toISOString(),
  agent: { agent: 'claude', model, effort: 'low', tier: 'small', launch_model: '' },
  ...over,
})
const four = () => [
  agent('a', 'Tooling audit: PDS model-facing tools', 'claude-haiku-5-5'),
  agent('b', 'Tooling audit: Storytime tools', 'claude-haiku-5-5', { status: 'failed', error: 'boom' }),
  agent('c', 'Tooling audit: Ergonaut engine tools', 'gpt-5.2-codex', {
    activity: {
      entries: [],
      source: 'transcript',
      at: Date.now() / 1000,
      checked_at: Date.now() / 1000,
      waiting: 'May I push?',
      liveness: '',
      stall_after: 900,
    },
  }),
  agent('d', 'Tooling audit: ergo-bots skills', ''),
]
const chips = () => Array.from(container.querySelectorAll<HTMLButtonElement>('[data-testid="worker-chip"]'))

describe('ActivityStrip', () => {
  test('shows one badge per worker on one line, with its short model, people first', () => {
    render([], four())
    const line = container.querySelector('[data-testid="activity-line"]')!
    expect(line.className).toContain('h-7')
    expect(line.textContent).toContain('4 agents')
    const all = chips()
    expect(all.length).toBe(4)
    // Waiting on you first, then the failed one, then work in progress.
    expect(all.map(c => c.dataset.state)).toEqual(['waiting', 'failed', 'running', 'running'])
    // The shared "Tooling audit: " goes, so the part that differs survives truncation.
    expect(all[0].textContent).toContain('Ergonaut engine tools')
    expect(all[0].textContent).not.toContain('Tooling audit')
    expect(all[0].getAttribute('aria-label')).toContain('Tooling audit: Ergonaut engine tools')
    expect(all.map(c => c.querySelector('[data-testid="chip-model"]')?.textContent)).toEqual([
      'gpt-5.2-codex',
      'Haiku 5.5',
      'Haiku 5.5',
      '?',
    ])
    expect(all[0].className).toContain('border-warning')
    // The tooltip has what the badge leaves out.
    const tip = all[2].title
    expect(tip).toContain('claude: Tooling audit: PDS model-facing tools')
    expect(tip).toContain('Effort: low')
    expect(tip).toContain('Tier: small')
    expect(tip).toContain('running for 5m')
    expect(all[0].title).toContain('asks: May I push?')
    const scroller = container.querySelector('[data-testid="worker-chips"]')!
    expect(scroller.className).toContain('overflow-x-auto')
    expect(scroller.className).not.toContain('flex-wrap')
    expect(container.querySelector('[data-testid="activity-panel"]')).toBeNull()
    render([request('answered'), request('failed')], [])
    expect(container.textContent).toBe('')
  })

  test('a badge opens that worker in the terminal', async () => {
    const asked: string[] = []
    api.workerLog = async (sessionId: string, workerId: string) => {
      asked.push(`${sessionId}/${workerId}`)
      return { id: workerId, title: '', source: 'terminal', live: true, error: '', entries: [] }
    }
    render([], four())
    expect(container.querySelector('dialog')).toBeNull()
    await act(async () => chips()[2].click())
    expect(asked).toEqual(['s1/a'])
    expect(container.querySelector('dialog')?.getAttribute('aria-label')).toBe(
      'Output of Tooling audit: PDS model-facing tools',
    )
    expect(container.querySelector('[data-testid="worker-terminal"]')).not.toBeNull()
  })

  test('badges past the edge get a +N that lists every worker', async () => {
    api.workerLog = async (_s: string, workerId: string) => ({
      id: workerId,
      title: '',
      source: 'terminal',
      live: true,
      error: '',
      entries: [],
    })
    const many = Array.from({ length: 8 }, (_, i) => agent(`w${i}`, `Job ${i}`, 'claude-haiku-5-5'))
    render([], many)
    expect(container.querySelector('[data-testid="chips-overflow"]')).toBeNull()
    // Lay the badges out 100px apart in a 350px line: the last five reach past it.
    const scroller = container.querySelector<HTMLElement>('[data-testid="worker-chips"]')!
    let scrolled = 0
    const rect = (left: number, width: number) => ({ left, right: left + width, width }) as DOMRect
    scroller.getBoundingClientRect = () => rect(0, 350)
    chips().forEach((chip, i) => {
      chip.getBoundingClientRect = () => rect(i * 100 - scrolled, 96)
    })
    act(() => {
      scroller.dispatchEvent(new window.Event('scroll') as unknown as Event)
    })
    const more = container.querySelector<HTMLButtonElement>('[data-testid="chips-overflow"]')!
    expect(more.textContent).toBe('+5')
    expect(scroller.className).toContain('activity-chips-fade')
    act(() => more.click())
    expect(more.getAttribute('aria-expanded')).toBe('true')
    const items = Array.from(container.querySelectorAll<HTMLButtonElement>('[role="menuitem"]'))
    expect(items.length).toBe(8)
    expect(items[7].textContent).toContain('Job 7')
    expect(items[7].textContent).toContain('Haiku 5.5')
    await act(async () => items[7].click())
    expect(container.querySelector('[data-testid="chips-popover"]')).toBeNull()
    expect(container.querySelector('dialog')?.getAttribute('aria-label')).toBe('Output of Job 7')
    // Scrolled to the end, nothing is past the edge.
    scrolled = 450
    act(() => {
      scroller.dispatchEvent(new window.Event('scroll') as unknown as Event)
    })
    expect(container.querySelector('[data-testid="chips-overflow"]')).toBeNull()
  })

  test('open requests sit behind a toggle on the line, remembered across remounts', () => {
    render([request('waiting')], [])
    let button = disclosure()
    expect(button.textContent).toContain('1 request')
    expect(button.type).toBe('button')
    expect(button.getAttribute('aria-expanded')).toBe('false')
    const panel = window.document.getElementById(button.getAttribute('aria-controls')!)!
    expect(panel.hidden).toBe(true)
    expect(panel.textContent).not.toContain('Activity details')
    act(() => button.click())
    expect(button.getAttribute('aria-expanded')).toBe('true')
    expect(panel.hidden).toBe(false)
    expect(panel.textContent).toContain('Activity details')
    expect(window.localStorage.getItem('ergonaut.activity.s1')).toBe('open')

    act(() => root.unmount())
    root = createRoot(container)
    render()
    button = disclosure()
    expect(button.getAttribute('aria-expanded')).toBe('true')
    act(() => button.click())
    expect(window.localStorage.getItem('ergonaut.activity.s1')).toBe('closed')
    // Another chat starts closed.
    render([request('waiting')], [], 'chat-b')
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
