import { afterEach, beforeEach, describe, expect, test } from 'bun:test'
import { act, createElement } from 'react'
import type { createRoot as CreateRoot, Root } from 'react-dom/client'
import { Window } from 'happy-dom'
import { MemoryRouter } from 'react-router-dom'
import { api, type Message, type Worker, type WorkerLog } from '../api'
import { Transcript } from './Transcript'
import { WorkerBadges } from './WorkerBadges'

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

const chips = () => Array.from(container.querySelectorAll<HTMLButtonElement>('[data-testid="worker-chip"]'))

/** A transcript where one assistant turn starts these workers, each in its own message. */
function transcript(workers: Worker[], between: Message[] = []) {
  const messages: Message[] = workers.flatMap((w, i) => [
    {
      line: i * 2,
      role: 'assistant',
      timestamp: null,
      blocks: [{ type: 'tool_use', id: `s${i}`, name: 'orca_start_worker', input: {} }],
    },
    {
      line: i * 2 + 1,
      role: 'user',
      timestamp: null,
      blocks: [{ type: 'tool_result', tool_use_id: `s${i}`, content: JSON.stringify({ id: w.id }) }],
    },
    ...(i === 0 ? between : []),
  ])
  act(() =>
    root.render(
      createElement(MemoryRouter, null, createElement(Transcript, { messages, calls: [], workers, sessionId: 's1' })),
    ),
  )
}

const four = () => [
  worker(),
  worker({ id: 'w2', title: 'claude: Tooling audit: Storytime tools' }),
  worker({
    id: 'w3',
    title: 'codex: Tooling audit: Ergonaut engine',
    agent: { agent: 'codex', model: 'gpt-5.2-codex', effort: 'medium', tier: '', launch_model: '' },
  }),
  worker({
    id: 'w4',
    title: 'omp: Tooling audit: ergo-bots skills',
    agent: { agent: 'omp', model: '', effort: '', tier: '', launch_model: '' },
  }),
]

describe('WorkerBadges', () => {
  test('a badge is state, short title and model; times and output only in its tooltip', () => {
    act(() => root.render(createElement(WorkerBadges, { workers: [worker()] })))
    const [chip] = chips()
    expect(chip.dataset.state).toBe('running')
    expect(chip.textContent).toBe('Tooling audit: PDS model-facing toolsHaiku 5.5')
    expect(chip.textContent).not.toContain('Plants') // no latest output inline
    expect(chip.textContent).not.toContain('12m') // no times inline
    expect(chip.title).toContain('running for 12m')
    expect(chip.title).toContain('Last output 1m ago')
    expect(chip.title).toContain('Latest: 42: # Plants stay in a phase')
    expect(chip.title).not.toContain('\x1b')
    expect(container.querySelector('dialog')).toBeNull()
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
    act(() => root.render(createElement(WorkerBadges, { workers: [worker()] })))
    await act(async () => chips()[0].click())
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

describe('workers in the transcript', () => {
  test('starts in a row collapse into one line of badges side by side, not a row each', async () => {
    api.workerLog = async (_s: string, workerId: string) => ({
      id: workerId,
      title: '',
      source: 'terminal',
      live: true,
      error: '',
      entries: [],
    })
    transcript(four())
    const runs = container.querySelectorAll('[data-testid="worker-run"]')
    expect(runs.length).toBe(1)
    expect(runs[0].className).toContain('h-7')
    const line = runs[0].querySelector('[data-testid="worker-chips"]')!
    expect(line.className).toContain('overflow-x-auto')
    expect(line.className).not.toContain('flex-wrap')
    expect(line.querySelectorAll('[data-testid="worker-chip"]').length).toBe(4)
    // Start order, the shared "Tooling audit: " dropped, short models.
    expect(chips().map(c => c.querySelector('[data-testid="chip-model"]')?.textContent)).toEqual([
      'Haiku 5.5',
      'Haiku 5.5',
      'gpt-5.2-codex',
      '?',
    ])
    expect(chips()[2].textContent).toContain('Ergonaut engine')
    expect(container.querySelector('[data-testid="worker-row"]')).toBeNull()
    expect(container.textContent).not.toContain('Plants') // no output lines
    expect(container.textContent).not.toContain('>_ log')
    await act(async () => chips()[1].click())
    expect(container.querySelector('dialog')?.getAttribute('aria-label')).toBe(
      'Output of Tooling audit: Storytime tools',
    )
  })

  test('a lone start is one badge; text between starts splits the line', () => {
    transcript([worker()])
    expect(container.querySelectorAll('[data-testid="worker-run"]').length).toBe(1)
    expect(chips().length).toBe(1)
    const said: Message = {
      line: 100,
      role: 'assistant',
      timestamp: null,
      blocks: [{ type: 'text', text: 'Now the rest.' }],
    }
    transcript(four(), [said])
    const runs = Array.from(container.querySelectorAll('[data-testid="worker-run"]'))
    expect(runs.map(r => r.querySelectorAll('[data-testid="worker-chip"]').length)).toEqual([1, 3])
  })
})
