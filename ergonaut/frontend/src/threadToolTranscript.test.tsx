import { describe, expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import type { Message, SentCard } from './api'
import { Transcript } from './components/Transcript'

const id = '12345678-1234-1234-1234-123456789abc'

function renderTool(
  name: string,
  input: unknown,
  content?: unknown,
  isError = false,
  sessionId = '',
  sent: SentCard[] = [],
) {
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
    createElement(MemoryRouter, null, createElement(Transcript, { messages, calls: [], sessionId, sent })),
  )
}

describe('messaging tool transcript', () => {
  test.each([
    'ergo_thread_send',
    'ergo_message_up',
    'ergo_thread_forward',
    'ergo_thread_resolve',
    'ergo_thread_archive',
    'ergo_thread_stop',
    'ergo_thread_list',
  ])('%s keeps model guidance in tool details', name => {
    const html = renderTool(name, {}, { note: 'Tell the model to say this' })
    const [card] = html.split('<summary class="cursor-pointer py-1">Tool details</summary>')
    expect(card).not.toContain('Tell the model to say this')
    expect(html).toContain('Tool details')
  })

  test('uses live message state rather than a stale queue position', () => {
    const card: SentCard = {
      message_id: 'msg',
      created_at: '',
      status: 'done',
      text: 'Request',
      reply: '',
      prs: [],
      thread: {
        id,
        title: 'Target',
        bot: 'helper',
        role: '',
        archived: false,
        attention: false,
        state: 'idle',
        started_by: '',
        started_by_id: '',
        working_for: 0,
        waiting_on: 0,
        workers_running: 0,
      },
    }
    const result = { message_id: 'msg', session_id: id, status: 'queued', queue_position: 1 }
    const handled = renderTool('ergo_thread_forward', {}, result, false, '', [card]).split('Tool details')[0]
    expect(handled).not.toContain('Queue position')
    expect(handled).not.toContain('queued')
    const unknown = renderTool('ergo_thread_forward', {}, result).split('Tool details')[0]
    expect(unknown).toContain('Queue position 1')
    expect(unknown).toContain('queued')
  })

  test('clamps message and note previews with an expansion control', () => {
    const html = renderTool('ergo_thread_forward', { message: 'one\ntwo\nthree', note: 'four\nfive\nsix' }, {})
    expect(html.match(/line-clamp-2/g)?.length).toBe(2)
    expect(html.split('Tool details')[0].match(/aria-expanded="false"/g)?.length).toBe(2)
  })

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

  test('a failed call names its target when the refusal carries it, and shortens a bare id when not', () => {
    const refusal = {
      error: 'kitchen · Groceries was started by boundcorp',
      session_id: id,
      title: 'kitchen · Groceries',
      bot: 'kitchen',
    }
    for (const name of ['ergo_thread_resolve', 'ergo_thread_stop', 'ergo_thread_send', 'ergo_thread_forward']) {
      const html = renderTool(name, { thread_id: id, thread: id, bot: 'kitchen' }, refusal, true)
      expect(html).toContain('Failed')
      expect(html).toContain(`href="/s/${id}"`)
      expect(html).toContain('>kitchen · Groceries</a>')
      expect(html).toContain('kitchen · Groceries was started by boundcorp')
    }
    const unknown = renderTool('ergo_thread_resolve', { thread_id: id }, `No thread ${id} in kitchen`, true)
    expect(unknown).toContain('Failed')
    expect(unknown).toContain(`href="/s/${id}"`)
    expect(unknown).toContain('>12345678<')
  })

  test('bot listing remains readable', () => {
    expect(renderTool('ergo_bot_list', {}, 'Bots you can message:\nResearch bot')).toContain('Research bot')
  })
})
