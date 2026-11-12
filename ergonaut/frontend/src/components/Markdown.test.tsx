import { describe, expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import Markdown from './Markdown'

const renderDocument = (text: string) => renderToStaticMarkup(createElement(Markdown, { text, mode: 'document' }))

describe('document Markdown', () => {
  test('renders GFM tables, task lists and strikethrough', () => {
    const html = renderDocument('| Name | Value |\n| --- | --- |\n| one | two |\n\n- [x] done\n\n~~old~~')
    expect(html).toContain('<table>')
    expect(html).toContain('<td>two</td>')
    expect(html).toContain('type="checkbox"')
    expect(html).toContain('checked=""')
    expect(html).toContain('<del>old</del>')
  })

  test('never mounts images or raw HTML', () => {
    const html = renderDocument(
      '![remote](https://example.com/tracker.png) ![relative](../tracker.png) ![data](data:image/png;base64,AA==)\n\n<img src="https://example.com/raw.png" onerror="alert(1)">\n\n<script>alert(1)</script>',
    )
    expect(html).not.toContain('<img')
    expect(html).not.toContain('<script>')
    expect(html).not.toContain('<link rel="preload"')
    expect(html).toContain('&lt;img')
    expect(html).toContain('&lt;script&gt;')
  })

  test('allows relative, fragment, HTTP(S), and mailto links', () => {
    const html = renderDocument(
      '[relative](../guide) [fragment](#section) [http](http://example.com) [https](https://example.com) [mail](mailto:a@example.com)',
    )
    expect(html).toContain('href="../guide"')
    expect(html).toContain('href="#section"')
    expect(html).toContain('href="http://example.com" target="_blank" rel="noopener noreferrer"')
    expect(html).toContain('href="https://example.com" target="_blank" rel="noopener noreferrer"')
    expect(html).toContain('href="mailto:a@example.com"')
  })

  test('rejects protocol-relative and unsafe schemes', () => {
    const html = renderDocument(
      '[network](//example.com) [js](javascript:alert) [mixed](JaVaScRiPt:alert) [encoded](java&#x73;cript:alert) [data](data:text/html,evil) [ftp](ftp://example.com)',
    )
    expect(html).not.toContain('<a ')
    expect(html).toContain('network')
    expect(html).toContain('js')
  })

  test('leaves chat mode image and link behavior intact', () => {
    const html = renderToStaticMarkup(
      createElement(Markdown, { text: '![image](/image.png) [link](https://example.com)' }),
    )
    expect(html).toContain('<img')
    expect(html).toContain('href="https://example.com" target="_blank" rel="noreferrer"')
  })
})
