import { describe, expect, test } from 'bun:test'
import { suggestionsFromMessages, statusSummary } from './chatLayout'

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
