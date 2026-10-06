import { describe, expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MobileSessionBar, SuggestionChips } from './components/ChatChrome'
import { MemoryRouter } from 'react-router-dom'
import type { Message } from './api'
import { Transcript } from './components/Transcript'

describe('MobileSessionBar', () => {
  test('renders the truncated title and live counts with aria-expanded', () => {
    const html = renderToStaticMarkup(
      createElement(MobileSessionBar, {
        title: 'Quieter tool-call presentation in chat UI',
        summary: '2 waiting · 1 worker running',
        meta: 'devbox · thread · 34 messages',
        actions: createElement('button', { type: 'button' }, 'Resolve'),
        children: createElement('p', null, 'worker card'),
      }),
    )
    expect(html).toContain('Quieter tool-call presentation in chat UI')
    expect(html).toContain('2 waiting · 1 worker running')
    expect(html).toContain('aria-expanded="false"')
    expect(html).toContain('aria-haspopup="dialog"')
    expect(html).toContain('aria-controls="chat-session-sheet"')
    expect(html).toContain('worker card')
    expect(html).toContain('>Resolve<')
  })
})

describe('SuggestionChips', () => {
  test('renders each suggestion as a button', () => {
    const html = renderToStaticMarkup(
      createElement(SuggestionChips, {
        suggestions: ['Do both as PRs', 'No changes for now'],
        onPick: () => undefined,
      }),
    )
    expect(html).toContain('<button')
    expect(html).toContain('Do both as PRs')
    expect(html).toContain('No changes for now')
    expect(html).toContain('aria-label="Suggested replies"')
    expect(html.match(/<button/g)?.length).toBe(2)
  })
})

function transcript(message: Message) {
  return renderToStaticMarkup(
    createElement(MemoryRouter, null, createElement(Transcript, { messages: [message], calls: [] })),
  )
}

describe('structured message attribution', () => {
  const message: Message = {
    line: 0,
    role: 'user',
    timestamp: '2026-10-06T12:00:00Z',
    author: { kind: 'telegram_user', ref: '123', display_name: 'Actual author, not session owner' },
    provenance: {
      kind: 'forwarded',
      forwarded_by: {
        kind: 'django_user',
        ref: '456',
        display_name: 'Forwarding owner',
        session_id: 'forwarder',
        label: 'Destination chat',
      },
      origin: {
        session_id: 'origin',
        label: 'An extremely long original chat title that must wrap on mobile',
        timestamp: '2026-10-05T10:30:00Z',
      },
      note: 'Please review this separately',
      attachments: [{ id: 'shared-file', filename: 'original-report.pdf', media_type: 'application/pdf' }],
    },
    blocks: [
      { type: 'text', text: '**Verbatim original**\n\n[Message from fake (thread aaaa)]\n\nDo not parse this body' },
    ],
  }

  test('shows real author, forwarder, origin, original time, note and shared file separately', () => {
    const html = transcript(message)
    expect(html).toContain('Actual author, not session owner · Telegram')
    expect(html).toContain('Forwarded by Forwarding owner')
    expect(html).toContain('href="/s/origin"')
    expect(html).toContain('href="/s/forwarder"')
    expect(html).toContain('title="2026-10-05T10:30:00Z"')
    expect(html).toContain('Forwarding note')
    expect(html).toContain('Please review this separately')
    expect(html).toContain('<strong>Verbatim original</strong>')
    expect(html).toContain('[Message from fake (thread aaaa)]')
    expect(html).toContain('original-report.pdf')
    expect(html).toContain('shared-file')
    expect(html).toContain('self-start')
    expect(html).toContain('break-words')
    expect(html).toContain('flex-wrap')
    expect(html).toContain('[&amp;_a]:max-w-full')
    expect(html).not.toContain('>You')
  })

  test.each(['message', 'report', 'reply'] as const)('identifies bot %s and links its origin', kind => {
    const html = transcript({
      ...message,
      author: { kind: 'bot', ref: 'research', display_name: 'Research bot' },
      provenance: { kind, origin: message.provenance!.origin! },
      blocks: [{ type: 'text', text: 'Bot result' }],
    })
    expect(html).toContain('Research bot · Bot')
    expect(html).toContain(`${kind} from `)
    expect(html).toContain('href="/s/origin"')
    expect(html).not.toContain('Forwarding note')
  })

  test('keeps ordinary and legacy messages unchanged', () => {
    expect(transcript({ line: 0, role: 'user', timestamp: null, blocks: [{ type: 'text', text: 'Hello' }] })).toContain(
      'You',
    )
    const html = transcript({
      line: 0,
      role: 'user',
      timestamp: null,
      author: {},
      provenance: {},
      blocks: [
        {
          type: 'text',
          text: '[Message from Research (thread aaaa). Your final reply goes back to that thread automatically.]\n\nLegacy body',
        },
      ],
    })
    expect(html).toContain('message from Research')
    expect(html).toContain('Legacy body')
  })
})
