import { describe, expect, test } from 'bun:test'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import type { Call, Message } from './api'
import { contextPlacement } from './contextPlacement'
import { Transcript } from './components/Transcript'

function message(line: number, role = 'assistant'): Message {
  return { line, role, blocks: [{ type: 'text', text: `Message ${line}` }], timestamp: null }
}

function call(id: string, first_sequence: number | null, last_sequence: number | null): Call {
  return {
    id,
    first_sequence,
    last_sequence,
    kind: 'chat_reply',
    status: last_sequence == null ? 'in_progress' : 'completed',
    request: '',
    response: null,
    error: '',
    model_name: '',
    input_tokens: 0,
    output_tokens: 0,
    turns_used: 1,
    pending_approvals: [],
    tools: [],
    created_at: '',
    context: {
      sections: [],
      compaction: null,
      native_messages: { count: 0, first_sequence: null },
      stubbed_results: 0,
      estimated_tokens: 0,
      context_window: 0,
      compact_at_tokens: null,
      prompt_tokens: 0,
      request_tokens: null,
      requests: null,
    },
  }
}

// The previous render-loop calculation is the compatibility oracle, not the optimized algorithm.
function originalPlacement(messages: Message[], calls: Call[]): Map<number, Call> {
  const placed = new Map<number, Call>()
  for (const message of messages) {
    if (message.role !== 'assistant') continue
    const owner = calls.find(
      c =>
        c.first_sequence != null && c.first_sequence <= message.line && (c.last_sequence ?? Infinity) >= message.line,
    )
    const last =
      owner &&
      messages
        .filter(
          m => m.role === 'assistant' && m.line >= owner.first_sequence! && m.line <= (owner.last_sequence ?? Infinity),
        )
        .slice(-1)[0]
    if (owner && last?.line === message.line) placed.set(message.line, owner)
  }
  return placed
}

function entries(placed: Map<number, Call>) {
  return [...placed].map(([line, owner]) => [line, owner.id]).sort((a, b) => Number(a[0]) - Number(b[0]))
}

describe('context placement', () => {
  test('uses inclusive boundaries, ignores non-assistants and handles empty or missing ranges', () => {
    const messages = [message(0), message(1, 'user'), message(2), message(3, 'tool'), message(7)]
    const calls = [call('missing', null, 20), call('zero', 0, 0), call('gap', 3, 6), call('end', 2, 2)]
    expect(entries(contextPlacement(messages, calls))).toEqual([
      [0, 'zero'],
      [2, 'end'],
    ])
    expect(contextPlacement([], calls).size).toBe(0)
    expect(contextPlacement(messages, []).size).toBe(0)
    expect(contextPlacement([message(0, 'user')], [call('user-only', 0, null)]).size).toBe(0)
    expect(contextPlacement(messages, [call('reversed', 7, 2)]).size).toBe(0)
  })

  test('retains first matching input-call priority, not start-order priority', () => {
    const messages = [message(2), message(4), message(6), message(8)]
    const calls = [call('first', 4, 6), call('second', 0, 8), call('third', 0, null)]
    expect(entries(contextPlacement(messages, calls))).toEqual([
      [6, 'first'],
      [8, 'second'],
    ])
    expect(entries(contextPlacement(messages, calls))).toEqual(entries(originalPlacement(messages, calls)))
  })

  test('does not place a context after merely the last assistant owned by an overlapping call', () => {
    const messages = [message(2), message(4), message(6), message(8)]
    const calls = [call('earlier', 6, 8), call('overlap', 0, 8)]
    expect(entries(contextPlacement(messages, calls))).toEqual([[8, 'earlier']])
    calls[0].context = null
    expect(entries(contextPlacement(messages, calls))).toEqual([[8, 'earlier']])
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <Transcript messages={messages} calls={calls} />
      </MemoryRouter>,
    )
    expect(html).not.toContain('>Context</button>')
  })

  test('moves an unfinished call to the latest loaded assistant, including tool-use messages', () => {
    const running = call('running', 0, null)
    const messages = [message(4), message(5, 'user')]
    expect(entries(contextPlacement(messages, [running]))).toEqual([[4, 'running']])
    messages.push({
      line: 6,
      role: 'assistant',
      timestamp: null,
      blocks: [{ type: 'tool_use', id: 't', name: 'read', input: {} }],
    })
    expect(entries(contextPlacement(messages, [running]))).toEqual([[6, 'running']])
    expect(entries(contextPlacement([message(0, 'user'), message(2), ...messages], [running]))).toEqual([
      [6, 'running'],
    ])
    expect(entries(contextPlacement(messages, [call('completed', 0, 4)]))).toEqual([[4, 'completed']])
  })

  test('renders the context button only under the final loaded assistant, before later user messages', () => {
    const messages = [message(0), message(2), message(3, 'user')]
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <Transcript messages={messages} calls={[call('turn', 0, 3)]} complete={false} />
      </MemoryRouter>,
    )
    const context = html.indexOf('>Context</button>')
    expect(context).toBeGreaterThan(html.indexOf('id="m-2"'))
    expect(context).toBeLessThan(html.indexOf('id="m-3"'))
    expect(html.match(/>Context<\/button>/g)?.length).toBe(1)
  })

  test('matches the original algorithm across deterministic overlapping and unfinished ranges', () => {
    let seed = 12345
    const random = (max: number) => {
      seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0
      return seed % max
    }
    for (let sample = 0; sample < 100; sample++) {
      const messages = Array.from({ length: 50 }, (_, index) => message(index * 2, random(3) ? 'assistant' : 'user'))
      const calls = Array.from({ length: 30 }, (_, index) =>
        call(String(index), random(6) ? random(100) : null, random(4) ? random(110) : null),
      )
      expect(entries(contextPlacement(messages, calls))).toEqual(entries(originalPlacement(messages, calls)))
    }
  })

  test('bounds sequence reads for many fully overlapping calls without timing assertions', () => {
    let reads = 0
    const messages = Array.from({ length: 2000 }, (_, index) => ({
      ...message(index),
      get line() {
        reads++
        return index
      },
    }))
    const calls = Array.from({ length: 1000 }, (_, index) => call(String(index), 0, null))
    expect(entries(contextPlacement(messages, calls))).toEqual([[1999, '0']])
    expect(reads).toBeLessThan(100_000)
  })
})
