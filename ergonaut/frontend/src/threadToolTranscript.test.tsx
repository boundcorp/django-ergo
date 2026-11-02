import { describe, expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import type { Message } from './api'
import { Transcript } from './components/Transcript'

const id = '12345678-1234-1234-1234-123456789abc'

function renderTool(name: string, input: unknown, content?: unknown, isError = false, sessionId = '') {
  const messages: Message[] = [
    { line: 0, role: 'assistant', timestamp: null, blocks: [{ type: 'tool_use', id: 'call', name, input }] },
  ]
  if (content !== undefined)
    messages.push({
      line: 1,
      role: 'user',
      timestamp: null,
      blocks: [{ type: 'tool_result', tool_use_id: 'call', content: JSON.stringify(content), is_error: isError }],
    })
  return renderToStaticMarkup(
    createElement(MemoryRouter, null, createElement(Transcript, { messages, calls: [], sessionId })),
  )
}

describe('messaging tool transcript', () => {
  test.each(['ergo_thread_send', 'ergo_message_up', 'ergo_thread_forward'])(
    '%s shows its target and delivery',
    name => {
      const html = renderTool(
        name,
        { thread: 'new', message: 'Please investigate' },
        {
          session_id: id,
          title: 'Research',
          bot: 'helper',
          status: 'queued',
          queue_position: 2,
          note: 'Delivered in order',
        },
      )
      expect(html).toContain(`href="/s/${id}"`)
      expect(html).toContain('Research')
      expect(html).toContain('helper')
      expect(html).toContain('queued')
      expect(html).toContain('Queue position 2')
      expect(html).toContain('Tool details')
    },
  )

  test('distinguishes new threads and shows forward notes and resolution summaries', () => {
    expect(renderTool('ergo_thread_send', { thread: 'new' }, { thread_id: id })).toContain('Started thread')
    expect(renderTool('ergo_thread_send', { thread: id }, { thread_id: id })).toContain('Messaged')
    expect(renderTool('ergo_thread_forward', { thread: id, note: 'Important context' }, { thread_id: id })).toContain(
      'Note: Important context',
    )
    expect(renderTool('ergo_thread_resolve', { thread_id: id, summary: 'Completed' }, { session_id: id })).toContain(
      'Summary: Completed',
    )
  })

  test('links old send and forward results by thread_id without live cards', () => {
    for (const [name, key] of [
      ['ergo_thread_send', 'sent_to'],
      ['ergo_thread_forward', 'forwarded_to'],
    ]) {
      const html = renderTool(name, {}, { thread_id: id, [key]: 'Old target', status: 'sent' })
      expect(html).toContain(`href="/s/${id}"`)
      expect(html).toContain('Old target')
    }
  })

  test('lists each chat with its own link and status, including old rows', () => {
    const html = renderTool('ergo_thread_list', {}, [
      { id, title: 'A chat', status: 'active', open_requests: 2, you_are_here: true },
      { id: 'another', title: 'Another chat', status: 'resolved' },
    ])
    expect(html).toContain(`href="/s/${id}"`)
    expect(html).toContain('href="/s/another"')
    expect(html).toContain('2 open requests')
    expect(html).toContain('You are here')
    expect(html).toContain('resolved')
    expect(renderTool('ergo_thread_list', {}, [])).toContain('No chats or threads found')
  })

  test.each(['ergo_thread_resolve', 'ergo_thread_archive'])('%s supports old text and target id', name => {
    const html = renderTool(name, { thread_id: id }, 'Resolved the thread. Its history stays readable.')
    expect(html).toContain(`href="/s/${id}"`)
    expect(html).toContain('Resolved the thread')
    expect(html).toContain('Tool details')
  })

  test('self-resolution links the current session and new metadata takes precedence', () => {
    expect(renderTool('ergo_thread_resolve', {}, 'Resolved here.', false, id)).toContain(`href="/s/${id}"`)
    const html = renderTool('ergo_thread_resolve', {}, { session_id: id, title: 'Finished', status: 'resolved' })
    expect(html).toContain('Finished')
    expect(html).toContain('resolved')
  })

  test('stop shows the actual outcome and cancelled count', () => {
    const html = renderTool(
      'ergo_thread_stop',
      { thread_id: id },
      { thread: 'Busy thread', stopped_running_turn: false, cancelled_messages: 2 },
    )
    expect(html).toContain(`href="/s/${id}"`)
    expect(html).toContain('No running turn stopped')
    expect(html).toContain('2 queued messages cancelled')
  })

  test('failure and running states are not presented as success or a fabricated link', () => {
    const failed = renderTool('ergo_thread_forward', { thread: 'new' }, 'Not permitted', true)
    expect(failed).toContain('Failed')
    expect(failed).toContain('Not permitted')
    expect(failed).not.toContain('href="/s/new"')
    expect(renderTool('ergo_thread_send', { thread: 'main' })).toContain('Running')
  })

  test('bot listing remains readable', () => {
    expect(renderTool('ergo_bot_list', {}, 'Bots you can message:\nResearch bot')).toContain('Research bot')
  })
})
