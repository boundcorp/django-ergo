import { describe, expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { ModelLink, MobileSessionBar, SuggestionChips, ThreadOptions } from './components/ChatChrome'
import { MemoryRouter } from 'react-router-dom'
import type { Message, Worker } from './api'
import { Transcript } from './components/Transcript'
import { EffortSlider, modelLabel } from './components/ModelPicker'
import { PinnedDashboard } from './pages/Chat'

test('an exited question is actionable even in the collapsed worker disclosure', () => {
  const worker: Worker = {
    id: 'worker-question',
    title: 'Prepare artifact',
    function: 'agent:local',
    status: 'completed',
    progress: 'Needs a new run; the agent process has exited',
    result: { status: 'needs_new_run', outputs: [{ path: 'artifact.txt' }] },
    error: '',
    created_at: '',
    completed_at: null,
  }
  const html = renderToStaticMarkup(
    createElement(
      MemoryRouter,
      null,
      createElement(Transcript, {
        calls: [],
        workers: [worker],
        messages: [
          {
            line: 1,
            role: 'assistant',
            timestamp: null,
            blocks: [{ type: 'tool_use', id: 'start', name: 'ergo_agent_start', input: {} }],
          },
          {
            line: 2,
            role: 'user',
            timestamp: null,
            blocks: [{ type: 'tool_result', tool_use_id: 'start', content: { id: worker.id } }],
          },
        ],
      }),
    ),
  )
  const summary = html.match(/<summary\b[^>]*>[\s\S]*?<\/summary>/)?.[0]
  expect(summary).toContain('Needs new run')
  expect(summary).not.toContain('completed')
  expect(html).not.toContain('text-success')
})

describe('MobileSessionBar', () => {
  test('renders the truncated title and live counts with aria-expanded', () => {
    const html = renderToStaticMarkup(
      createElement(MobileSessionBar, {
        bot: 'devbox',
        open: false,
        onOpenChange: () => undefined,
        options: createElement('select', { 'aria-label': 'Model and effort' }),
        title: 'Quieter tool-call presentation in chat UI',
        summary: '2 waiting · 1 worker running',
        meta: 'devbox · thread · 34 messages',
        actions: createElement('button', { type: 'button' }, 'Resolve'),
        children: createElement('p', null, 'worker card'),
      }),
    )
    expect(html).toContain('Quieter tool-call presentation in chat UI')
    expect(html).toContain('class="chat-mobile-bot">devbox<')
    expect(html).toContain('aria-label="devbox, Quieter tool-call presentation in chat UI.')
    expect(html).toContain('aria-label="Model and effort"')
    expect(html).toContain('2 waiting · 1 worker running')
    expect(html).toContain('aria-expanded="false"')
    expect(html).toContain('aria-haspopup="dialog"')
    expect(html).toContain('aria-controls="chat-session-sheet"')
    expect(html).toContain('worker card')
    expect(html).toContain('>Resolve<')
  })
})

describe('SuggestionChips', () => {
  test('renders each suggestion as a focusable hold-to-send button', () => {
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
    expect(html).toContain('Hold for 0.7 seconds to send.')
    expect(html).toContain('aria-describedby=')
    expect(html).toContain('quick-reply-progress')
    expect(html.match(/<button/g)?.length).toBe(2)
  })
})

describe('PinnedDashboard', () => {
  test('shows the selected dashboard instead of the transcript panel', () => {
    const originalDocument = Object.getOwnPropertyDescriptor(globalThis, 'document')
    Object.defineProperty(globalThis, 'document', {
      configurable: true,
      value: { documentElement: { classList: { contains: () => false } } },
    })
    try {
      const html = renderToStaticMarkup(
        createElement(PinnedDashboard, {
          pin: { kind: 'bot_file', name: 'Account limits', url: '/api/bots/devbox/files/pages/limits.jhtml' },
          bot: 'devbox',
          sessionId: 'session-1',
          refreshKey: 3,
          onClose: () => undefined,
        }),
      )
      expect(html).toContain('title="Account limits"')
      expect(html).toContain('src="/api/bots/devbox/files/pages/limits.jhtml?theme=light"')
      expect(html).toContain('Back to chat')
    } finally {
      if (originalDocument) Object.defineProperty(globalThis, 'document', originalDocument)
      else Reflect.deleteProperty(globalThis, 'document')
    }
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

  test('separates GitHub events from Lee’s messages and keeps event bodies and attachments', () => {
    const html = renderToStaticMarkup(
      createElement(
        MemoryRouter,
        null,
        createElement(Transcript, {
          calls: [],
          messages: [
            {
              line: 0,
              role: 'user',
              timestamp: null,
              author: { kind: 'django_user', ref: 'lee', display_name: 'Lee' },
              blocks: [{ type: 'text', text: 'Please review my pull request.' }],
            },
            {
              line: 1,
              role: 'user',
              timestamp: null,
              author: { kind: 'system', ref: 'github', display_name: 'GitHub' },
              provenance: { kind: 'event' },
              blocks: [
                { type: 'text', text: '**Pull request updated**' },
                { type: 'attachment', id: 'event-file', label: 'check-log.txt' },
              ],
            },
          ],
        }),
      ),
    )
    const event = html.match(/<aside\b[^>]*>[\s\S]*?<\/aside>/)?.[0]
    expect(event).toBeDefined()
    expect(event!).toContain('aria-label="Automated event from GitHub"')
    expect(event!).toContain('GitHub · Event')
    expect(event!).toContain('Automated notification, not a message from the user.')
    expect(event!).toContain('<strong>Pull request updated</strong>')
    expect(event!).toContain('check-log.txt')
    expect(event!).toContain('event-file')
    expect(event!).not.toContain('Lee')
    expect(event!).not.toContain('>You')
    expect(html.replace(event!, '')).toContain('>Lee<')
    expect(html.replace(event!, '')).toContain('Please review my pull request.')
  })

  test('collapses a long worker result to its summary with the full text behind a disclosure', () => {
    const result = {
      summary: `Both catalog checks returned HTTP 200.\n\n${'Preset voices follow. '.repeat(40)}`,
      outputs: [{ path: 'out/catalog.md', size: 2774, sha256: 'abc123' }],
    }
    const html = renderToStaticMarkup(
      createElement(
        MemoryRouter,
        null,
        createElement(Transcript, {
          calls: [],
          messages: [
            {
              line: 0,
              role: 'user',
              timestamp: null,
              author: { kind: 'system', ref: 'worker:1', display_name: 'Worker: Catalog' },
              provenance: { kind: 'event' },
              blocks: [
                { type: 'text', text: `Worker “Catalog” finished.\n\nResult:\n${JSON.stringify(result, null, 2)}` },
              ],
            },
          ] as Message[],
        }),
      ),
    )
    const event = html.match(/<aside\b[^>]*>[\s\S]*?<\/aside>/)![0]
    const [shown, hidden] = event.split('<details')
    expect(shown).toContain('Both catalog checks returned HTTP 200.')
    expect(shown).not.toContain('out/catalog.md')
    expect(hidden).toContain('Show full text')
    expect(hidden).toContain('out/catalog.md')
  })

  test.each([
    { author: { kind: 'system', ref: 'github', display_name: 'GitHub' } },
    { provenance: { kind: 'event' } },
    {
      author: { kind: 'django_user', ref: 'lee', display_name: 'Lee' },
      provenance: { kind: 'event' },
    },
  ] satisfies Partial<Message>[])('recognizes event identity independently of its user role: %j', identity => {
    const html = transcript({
      line: 0,
      role: 'user',
      timestamp: null,
      blocks: [{ type: 'text', text: 'Automated update' }],
      ...identity,
    })
    expect(html).toContain('<aside')
    expect(html).toContain(' · Event')
    expect(html).not.toContain('>You')
    expect(html).not.toContain('Lee')
  })

  test('keeps real Telegram messages attributed to the human, not an event', () => {
    const html = transcript({
      line: 0,
      role: 'user',
      timestamp: null,
      author: { kind: 'telegram_user', ref: '123', display_name: 'Lee' },
      blocks: [{ type: 'text', text: 'I sent this from Telegram.' }],
    })
    expect(html).toContain('Lee · Telegram')
    expect(html).toContain('/api/authors/telegram_user/123/avatar')
    expect(html).not.toContain('<aside')
    expect(html).not.toContain(' · Event')
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

  test('shows a legacy cross-bot report as inbound rather than the session owner', () => {
    const html = transcript({
      line: 0,
      role: 'user',
      timestamp: '2026-10-06T12:43:00Z',
      author: {},
      provenance: {},
      blocks: [
        {
          type: 'text',
          text: '[Report from devbox · Main (thread 00000000-0000-0000-0000-000000000001). No reply goes back to it: act on it if it needs action, and tell the user what matters.]\n\nI’ve started a worker on the suggestion-click bug.',
        },
      ],
    })
    expect(html).toContain('Report from devbox · Main')
    expect(html).toContain('I’ve started a worker on the suggestion-click bug.')
    expect(html).toContain('self-start')
    expect(html).toContain('bg-teal-tint')
    expect(html).not.toContain('>You')
  })
})

describe('ThreadOptions', () => {
  const render = (open: boolean) =>
    renderToStaticMarkup(
      createElement(ThreadOptions, { open, onOpenChange: () => undefined }, createElement('select', null)),
    )

  test('keeps the picker out of the page until opened', () => {
    const closed = render(false)
    expect(closed).toContain('aria-expanded="false"')
    expect(closed).not.toContain('<select')
  })

  test('shows the picker in a labelled group when open', () => {
    const open = render(true)
    expect(open).toContain('aria-expanded="true"')
    expect(open).toContain('aria-label="Thread options"')
    expect(open).toContain('<select')
  })
})

describe('ModelLink', () => {
  test('describes the model as a button', () => {
    const html = renderToStaticMarkup(createElement(ModelLink, { label: 'Sonnet 5.5', onOpen: () => undefined }))
    expect(html).toContain('<button')
    expect(html).toContain('Model: Sonnet 5.5')
  })
})

describe('modelLabel', () => {
  const models = {
    default: 'anthropic/sonnet-5-5',
    default_engine_type: 'claude',
    models: [
      {
        id: 'anthropic/sonnet-5-5',
        name: 'sonnet-5-5',
        label: 'Sonnet 5.5',
        provider: 'anthropic',
        engine_type: 'claude',
        available: true,
      },
      { id: 'openai/gpt-6', name: 'gpt-6', label: 'GPT-6', provider: 'openai', engine_type: 'openai', available: true },
    ],
  }

  test('names the bot default when the chat has no model picked', () => {
    expect(modelLabel(models, '')).toBe('Sonnet 5.5 (default)')
  })

  test('uses the picked model label, or the raw id when it is not listed', () => {
    expect(modelLabel(models, 'openai/gpt-6')).toBe('GPT-6')
    expect(modelLabel(models, 'auto/medium')).toBe('auto/medium')
  })
})

describe('EffortSlider', () => {
  test('shows medium as the default when the chat has none picked', () => {
    const html = renderToStaticMarkup(createElement(EffortSlider, { value: '', onPick: () => undefined }))
    expect(html).toContain('aria-label="Reasoning effort"')
    expect(html).toContain('aria-valuetext="medium"')
    expect(html).toContain('medium (default)')
  })

  test('places a picked effort on its step', () => {
    const html = renderToStaticMarkup(createElement(EffortSlider, { value: 'xhigh', onPick: () => undefined }))
    expect(html).toContain('value="3"')
    expect(html).not.toContain('(default)')
  })
})
