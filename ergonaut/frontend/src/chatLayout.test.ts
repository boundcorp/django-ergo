import { describe, expect, test } from 'bun:test'
import { lastTurn, suggestionsFromMessages, statusSummary } from './chatLayout'
import { clock } from './time'

describe('statusSummary', () => {
  test('counts waiting threads and a running worker', () => {
    const requests = [
      { direction: 'out', status: 'delivered' },
      { direction: 'out', status: 'waiting' },
      { direction: 'out', status: 'answered' },
      { direction: 'in', status: 'delivered' },
    ]
    const workers = [{ status: 'running' }, { status: 'queued' }, { status: 'completed' }]
    expect(statusSummary(requests, workers)).toBe('2 waiting · 1 working for you · 2 workers running')
  })

  test('uses the singular worker label and includes pins', () => {
    expect(statusSummary([{ direction: 'out', status: 'queued' }], [{ status: 'running' }], 1)).toBe(
      '1 waiting · 1 worker running · 1 pinned',
    )
  })

  test('shows an unfinished todo list, not a finished one', () => {
    const todos = [{ status: 'completed' }, { status: 'in_progress' }, { status: 'pending' }]
    expect(statusSummary([], [], 0, todos)).toBe('1/3 todos done')
    expect(statusSummary([], [], 0, [{ status: 'completed' }])).toBe('')
  })

  test('shows when a pending self-wake is due', () => {
    const dueAt = '2026-11-07T15:30:00Z'
    expect(statusSummary([], [], 0, [], [{ due_at: dueAt }])).toBe(`wakes ${clock(dueAt)}`)
  })

  test('is empty when nothing is pending', () => {
    expect(statusSummary([{ direction: 'out', status: 'answered' }], [{ status: 'completed' }], 0)).toBe('')
  })
})

describe('suggestionsFromMessages', () => {
  test('reads suggestions from the latest send_reply call', () => {
    const messages = [
      {
        blocks: [{ type: 'tool_use', name: 'send_reply', input: { suggestions: ['Older'] } }],
      },
      {
        blocks: [
          { type: 'text', text: 'hi' },
          { type: 'tool_use', name: 'send_reply', input: { text: 'ok', suggestions: ['Do both', 'Wait'] } },
        ],
      },
    ]
    expect(suggestionsFromMessages(messages)).toEqual(['Do both', 'Wait'])
  })

  test('ignores other tool calls', () => {
    expect(suggestionsFromMessages([{ blocks: [{ type: 'tool_use', name: 'ergo_worker_list', input: {} }] }])).toBe(
      undefined,
    )
  })
})

describe('lastTurn', () => {
  test('skips side calls recorded after the turn', () => {
    const calls = [
      { id: 'a', kind: 'chat_reply' },
      { id: 'b', kind: 'chat_reply' },
      { id: 'c', kind: 'voice_transcription' },
    ]
    expect(lastTurn(calls)?.id).toBe('b')
  })

  test('is undefined without a turn', () => {
    expect(lastTurn([{ kind: 'illustrate_suggest' }])).toBeUndefined()
    expect(lastTurn([])).toBeUndefined()
  })
})
