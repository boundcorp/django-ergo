// @vitest-environment node
import { readFileSync } from 'node:fs'
import { renderToString } from 'react-dom/server'
import { describe, expect, test } from 'vitest'
import { ChatView, safeAttachmentUrl, safeLinkUrl, type Message } from '../src'

const messages: Message[] = [
  {
    id: '1',
    role: 'assistant',
    parts: [
      { type: 'text', text: 'hello ```js\nlet a = 1\n```' },
      { type: 'tool', id: 't', name: 'x', status: 'running', input: {} },
    ],
  },
]

describe('SSR', () => {
  test('renders on the server with no window or document', () => {
    expect(typeof window).toBe('undefined')
    const html = renderToString(<ChatView messages={messages} onSend={() => {}} onAttach={async () => []} />)
    expect(html).toContain('ergo-chat__tool')
    expect(html).toContain('Attach files')
  })
  test('server output is stable between renders (hydration ids)', () => {
    const a = renderToString(<ChatView messages={messages} onSend={() => {}} />)
    const b = renderToString(<ChatView messages={messages} onSend={() => {}} />)
    expect(a).toBe(b)
  })
})

describe('styles.css is scoped', () => {
  const css = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '')
  const selectors = [...css.matchAll(/(?:^|})\s*([^{}@]+)\{/g)]
    .flatMap(m => m[1].split(',').map(s => s.trim()))
    .filter(Boolean)
  test('every selector starts with .ergo-chat', () => {
    const stray = selectors.filter(
      s => !/^(\.ergo-chat|video\.ergo-chat)/.test(s) && !/^(from|to|\d+%)$/.test(s) && s !== '50%',
    )
    expect(stray).toEqual([])
    expect(selectors.length).toBeGreaterThan(50)
  })
  test('no global rules, no font-family declarations other than inherit/mono token', () => {
    expect(css).not.toMatch(/:root|@import|@font-face|@tailwind|@layer/)
    const families = [...css.matchAll(/font-family:\s*([^;]+);/g)].map(m => m[1].trim())
    expect(families.every(f => f === 'inherit' || f === 'var(--ergo-chat-mono)')).toBe(true)
  })
})

describe('url safety', () => {
  test.each([
    ['https://a.test/x', 'https://a.test/x'],
    ['mailto:a@b.test', 'mailto:a@b.test'],
    ['/path?q=1', '/path?q=1'],
    ['#frag', '#frag'],
    ['javascript:alert(1)', undefined],
    ['JaVaScRiPt:alert(1)', undefined],
    [' javascript:alert(1)', undefined],
    ['java\nscript:alert(1)', undefined],
    ['data:text/html,<b>', undefined],
    ['//evil.test/x', undefined],
    ['\\\\evil.test', undefined],
    ['vbscript:x', undefined],
    ['', undefined],
  ])('link %j', (input, expected) => expect(safeLinkUrl(input)).toBe(expected))
  test('attachment urls allow blob and raster data images only', () => {
    expect(safeAttachmentUrl('blob:https://a.test/uuid')).toBe('blob:https://a.test/uuid')
    expect(safeAttachmentUrl('data:image/png;base64,AAAA')).toBe('data:image/png;base64,AAAA')
    expect(safeAttachmentUrl('data:image/svg+xml;base64,AAAA')).toBeUndefined()
    expect(safeAttachmentUrl('data:text/html;base64,AAAA')).toBeUndefined()
  })
})
