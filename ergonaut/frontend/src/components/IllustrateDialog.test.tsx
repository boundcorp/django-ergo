import { describe, expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { IllustrateDialog } from './IllustrateDialog'

describe('IllustrateDialog', () => {
  test('offers an accessible custom-only request field', () => {
    const html = renderToStaticMarkup(
      createElement(IllustrateDialog, { sessionId: 'thread-1', disabled: false, onCreated: () => undefined }),
    )
    expect(html).toContain('Illustrate this chat')
    expect(html).toContain('Something else')
    expect(html).toContain('id="illustrate-custom"')
    expect(html).toContain('maxLength="500"')
    expect(html).toContain('Uses this chat snapshot only.')
    expect(html).toContain('Create illustration')
  })
})
