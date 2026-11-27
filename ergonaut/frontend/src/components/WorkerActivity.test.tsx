import { afterEach, beforeEach, describe, expect, test } from 'bun:test'
import { act, createElement } from 'react'
import type { createRoot as CreateRoot, Root } from 'react-dom/client'
import { Window } from 'happy-dom'
import { api, type Worker, type WorkerLog } from '../api'
import { WorkerRow } from './WorkerActivity'

const LONG = 'grep -n "page|lastModified|limit" ' + 'x'.repeat(400)

const worker = (over: Partial<Worker> = {}): Worker => ({
  id: 'w1',
  session_id: 's1',
  title: 'claude: Tooling audit: PDS model-facing tools',
  function: 'agent:orca',
  status: 'running',
  progress: 'ready · screen: busy',
  result: null,
  error: '',
  created_at: new Date(Date.now() - 12 * 60_000).toISOString(),
  completed_at: null,
  activity: {
    entries: [
      { kind: 'tool', text: `\x1b[1mBash\x1b[0m ${LONG}`, at: null },
      { kind: 'result', text: '7: lastModifiedStart\n42: # Plants stay   in a phase', at: null },
    ],
    source: 'transcript',
    at: Date.now() / 1000 - 60,
    checked_at: Date.now() / 1000,
    waiting: '',
    liveness: '',
    stall_after: 900,
  },
  agent: { agent: 'claude', model: 'claude-haiku-5-5', effort: 'low', tier: 'small', launch_model: '' },
  ...over,
})

let window: Window
let root: Root
let createRoot: typeof CreateRoot
let container: HTMLDivElement
const originalLog = api.workerLog

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
  createRoot = (await import('react-dom/client')).createRoot
  container = window.document.createElement('div')
  window.document.body.append(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => root.unmount())
  api.workerLog = originalLog
  window.happyDOM.close()
})

describe('WorkerRow', () => {
  test('shows state, model, title and one clean line of the latest output', () => {
    act(() => root.render(createElement(WorkerRow, { worker: worker() })))
    const row = container.querySelector('[data-testid="worker-row"]')!
    expect(row.textContent).toContain('Running')
    expect(row.textContent).toContain('Tooling audit: PDS model-facing tools')
    expect(row.textContent).not.toContain('claude: Tooling') // the agent is in the model chip
    expect(container.querySelector('[data-testid="worker-model"]')!.textContent).toBe('claude · Haiku 5.5 · low')
    expect(row.textContent).toContain('42: # Plants stay in a phase')
    expect(row.textContent).not.toContain('\x1b')
    expect(row.textContent).toContain('12m')
    expect(row.textContent).toContain('out 1m ago')
    expect(container.querySelector('dialog')).toBeNull()
  })

  test('says so when an older worker never recorded its model, and shows its question', () => {
    const old = worker({
      agent: { agent: 'codex', model: '', effort: '', tier: '', launch_model: '' },
      activity: { ...worker().activity!, waiting: 'Merge PR #12?' },
    })
    act(() => root.render(createElement(WorkerRow, { worker: old })))
    expect(container.querySelector('[data-testid="worker-model"]')!.textContent).toBe('codex · model unknown')
    expect(container.textContent).toContain('Waiting on you')
    expect(container.textContent).toContain('asks: Merge PR #12?')
  })

  test('opens a terminal that keeps line breaks and long lines and follows the end', async () => {
    const log: WorkerLog = {
      id: 'w1',
      title: '',
      source: 'terminal',
      live: true,
      error: '',
      entries: [
        { kind: 'terminal', text: `$ ${LONG}`, at: null },
        { kind: 'terminal', text: '\x1b[32mok\x1b[0m\nsecond line', at: null },
      ],
    }
    const asked: string[] = []
    api.workerLog = async (sessionId: string, workerId: string) => {
      asked.push(`${sessionId}/${workerId}`)
      return log
    }
    act(() => root.render(createElement(WorkerRow, { worker: worker() })))
    const button = container.querySelector<HTMLButtonElement>('button[aria-label^="Open output"]')!
    await act(async () => button.click())
    expect(asked).toEqual(['s1/w1'])
    const terminal = container.querySelector('[data-testid="worker-terminal"]')!
    expect(terminal.className).toContain('overflow-auto')
    expect(terminal.className).toContain('font-mono')
    expect(terminal.textContent).toContain(LONG) // not truncated
    expect(terminal.textContent).toContain('ok\nsecond line')
    expect(terminal.textContent).not.toContain('\x1b')
    expect(terminal.querySelector('.whitespace-pre')).not.toBeNull()
    expect(container.textContent).toContain('refreshes every 5s')
  })
})
