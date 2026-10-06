import { describe, expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { ToolCard } from './components/ToolCard'

const use = { type: 'tool_use' as const, id: 'call_1', name: 'bash', input: { command: 'ls -la' } }

describe('ToolCard', () => {
  test('starts folded with a one-line summary', () => {
    const html = renderToStaticMarkup(
      createElement(ToolCard, {
        use,
        result: { type: 'tool_result', tool_use_id: 'call_1', content: 'total 0' },
        duration: 1200,
      }),
    )
    expect(html).toContain('aria-expanded="false"')
    expect(html).toContain('command=ls -la')
    expect(html).toContain('1.2s')
    expect(html).not.toContain('Arguments')
    expect(html).not.toContain('Result')
  })

  test('a failed call is folded too, with its status in the header', () => {
    const html = renderToStaticMarkup(
      createElement(ToolCard, {
        use,
        result: { type: 'tool_result', tool_use_id: 'call_1', content: 'exit 1', is_error: true },
      }),
    )
    expect(html).toContain('aria-expanded="false"')
    expect(html).toContain('error:')
    expect(html).not.toContain('exit 1')
  })
})
