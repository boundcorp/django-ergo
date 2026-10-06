import { describe, expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MobileSessionBar, SuggestionChips } from './components/ChatChrome'

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
