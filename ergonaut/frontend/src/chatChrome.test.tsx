import { describe, expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { ModelLink, MobileSessionBar, SuggestionChips, ThreadOptions } from './components/ChatChrome'
import { modelLabel } from './components/ModelPicker'

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
