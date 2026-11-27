import { describe, expect, test } from 'bun:test'
import type { Worker, WorkerAgent } from './api'
import {
  agentLabel,
  cleanLine,
  latestLine,
  modelName,
  stripAnsi,
  workerState,
  workerTitle,
  workersSummary,
} from './workerDisplay'

const agent = (over: Partial<WorkerAgent> = {}): WorkerAgent => ({
  agent: 'claude',
  model: '',
  effort: '',
  tier: '',
  launch_model: '',
  ...over,
})
const worker = (over: Partial<Worker> = {}): Worker => ({
  id: 'w',
  title: 'claude: Tooling audit',
  function: 'agent:orca',
  status: 'running',
  progress: '',
  result: null,
  error: '',
  created_at: '2026-11-27T10:00:00Z',
  completed_at: null,
  ...over,
})

describe('terminal text', () => {
  test('strips escape codes and controls but keeps line breaks', () => {
    expect(stripAnsi('\x1b[1;32mok\x1b[0m\r\nnext\x07\tline')).toBe('ok\nnext  line')
    expect(stripAnsi('\x1b]0;title\x07shown')).toBe('shown')
  })

  test('one clean line collapses whitespace', () => {
    expect(cleanLine('  \x1b[2mBash\x1b[0m   grep -n\n  "x"  ')).toBe('Bash grep -n "x"')
  })

  test('the latest line skips borders, spinners and blank output', () => {
    const entries = [
      { kind: 'tool', text: 'Bash pytest -q', at: null },
      { kind: 'result', text: '12 passed\n\n───────', at: null },
      { kind: 'terminal', text: '  ⠋  ', at: null },
    ]
    expect(latestLine(entries)).toBe('12 passed')
    expect(latestLine([{ kind: 'error', text: 'boom happened', at: null }])).toBe('✗ boom happened')
    expect(latestLine([])).toBe('')
    expect(latestLine(undefined)).toBe('')
  })
})

describe('model labels', () => {
  test('names Claude models as people say them, others as given', () => {
    expect(modelName('claude-haiku-5-5')).toBe('Haiku 5.5')
    expect(modelName('anthropic/claude-sonnet-5-5')).toBe('Sonnet 5.5')
    expect(modelName('claude-opus-5-5-20261101')).toBe('Opus 5.5')
    expect(modelName('gpt-5.2-codex')).toBe('gpt-5.2-codex')
    expect(modelName('xai/grok-4.7')).toBe('grok-4.7')
  })

  test('prefers the observed launch model and says when the model is unknown', () => {
    expect(agentLabel(agent({ model: 'claude-haiku-5-5', effort: 'low' }))).toBe('claude · Haiku 5.5 · low')
    expect(agentLabel(agent({ model: 'a', launch_model: 'anthropic/claude-sonnet-5-5', tier: 'small' }))).toBe(
      'claude · Sonnet 5.5 · small',
    )
    expect(agentLabel(agent(), false)).toBe('model unknown')
  })

  test('drops the agent prefix from titles only when the agent is shown', () => {
    expect(workerTitle(worker({ agent: agent() }))).toBe('Tooling audit')
    expect(workerTitle(worker())).toBe('claude: Tooling audit')
  })
})

describe('worker state', () => {
  const now = Date.parse('2026-11-27T10:30:00Z') / 1000
  test('a question beats a stall, and a needs-new-run result is not done', () => {
    const activity = { entries: [], source: '', at: now - 3600, checked_at: now, liveness: '', stall_after: 600 }
    expect(workerState(worker({ activity: { ...activity, waiting: 'Approve?' } }), now)).toBe('waiting')
    expect(workerState(worker({ activity: { ...activity, waiting: '' } }), now)).toBe('stalled')
    expect(workerState(worker({ activity: { ...activity, waiting: '', at: now } }), now)).toBe('running')
    expect(workerState(worker({ status: 'completed', result: { status: 'needs_new_run' } }), now)).toBe('needs_run')
    expect(workerState(worker({ status: 'completed' }), now)).toBe('done')
    expect(workerState(worker({ status: 'failed' }), now)).toBe('failed')
  })

  test('summarizes agents by state', () => {
    const agents = [
      worker({ agent: agent() }),
      worker({ agent: agent() }),
      worker({ agent: agent(), status: 'failed' }),
    ]
    expect(workersSummary(agents, now)).toBe('2 agents running · 1 failed')
    expect(workersSummary([worker()], now)).toBe('1 worker running')
  })
})
