import { afterEach, beforeEach, describe, expect, test } from 'bun:test'
import { act, createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import type { Root } from 'react-dom/client'
import { Window } from 'happy-dom'
import { AttachmentView } from './MediaGallery'

const originalFetch = globalThis.fetch
let win: Window
let root: Root
let container: HTMLDivElement

beforeEach(async () => {
  win = new Window({ url: 'http://localhost' })
  // React DOM binds to the document at import time; install the test DOM first.
  Object.assign(globalThis, {
    window: win,
    document: win.document,
    navigator: win.navigator,
    HTMLElement: win.HTMLElement,
    Event: win.Event,
    MouseEvent: win.MouseEvent,
    IS_REACT_ACT_ENVIRONMENT: true,
  })
  const { createRoot } = await import('react-dom/client')
  container = win.document.createElement('div')
  win.document.body.append(container)
  root = createRoot(container)
})
afterEach(() => {
  act(() => root.unmount())
  globalThis.fetch = originalFetch
  win.happyDOM.close()
})

function button(name: string) {
  const found = [...win.document.querySelectorAll('button')].find(
    b => b.getAttribute('aria-label') === name || b.textContent === name,
  )
  if (!found) throw new Error(`Missing button ${name}`)
  return found
}
async function click(name: string) {
  await act(async () => {
    button(name).click()
    await Bun.sleep(0)
  })
}

describe('Markdown attachments', () => {
  test('recognizes filename and MIME but leaves missing and media attachments unchanged', () => {
    for (const props of [
      { id: 'one', label: 'NOTES.MARKDOWN' },
      { id: 'two', label: 'notes', media_type: 'text/markdown; charset=utf-8' },
    ]) {
      const html = renderToStaticMarkup(createElement(AttachmentView, props))
      expect(html).toContain('Markdown document')
      expect(html).toContain(`/api/attachments/${props.id}/download`)
    }
    expect(renderToStaticMarkup(createElement(AttachmentView, { label: 'missing.md' }))).not.toContain(
      'Markdown document',
    )
    expect(
      renderToStaticMarkup(createElement(AttachmentView, { id: 'x', label: 'photo.md', kind: 'image' })),
    ).toContain('<img')
  })

  test('fetches only after opening and copies exact source', async () => {
    const raw = '# Heading\r\n\r\n| a | b |\r\n| - | - |\r\n| 1 | 2 |\r\n'
    let calls = 0
    let copied = ''
    globalThis.fetch = (async () => {
      calls++
      return new Response(raw)
    }) as typeof fetch
    Object.defineProperty(win.navigator, 'clipboard', {
      configurable: true,
      value: {
        writeText: async (text: string) => {
          copied = text
        },
      },
    })
    act(() => root.render(createElement(AttachmentView, { id: 'one', label: 'notes.md' })))
    expect(calls).toBe(0)
    await click('Read notes.md')
    expect(calls).toBe(1)
    expect(win.document.querySelector('dialog h1')?.textContent).toBe('Heading')
    expect(win.document.querySelector('dialog a')?.getAttribute('href')).toBe('/api/attachments/one/download')
    await click('Copy raw source')
    expect(copied).toBe(raw)
    expect(win.document.body.textContent).toContain('Raw source copied.')
  })

  test('keeps download on fetch and clipboard failures', async () => {
    globalThis.fetch = (async () => new Response('', { status: 404 })) as typeof fetch
    act(() => root.render(createElement(AttachmentView, { id: 'one', label: 'notes.md' })))
    await click('Read notes.md')
    expect(win.document.querySelector('[role=alert]')?.textContent).toContain('Could not load')
    expect(win.document.querySelector('dialog a')?.getAttribute('href')).toBe('/api/attachments/one/download')
    await click('Close reader')
    globalThis.fetch = (async () => new Response('# source')) as typeof fetch
    Object.defineProperty(win.navigator, 'clipboard', {
      configurable: true,
      value: {
        writeText: async () => {
          throw Error('denied')
        },
      },
    })
    await click('Read notes.md')
    await click('Copy raw source')
    expect(win.document.body.textContent).toContain('Could not copy raw source.')
  })

  test('rejects oversized streams and leaves the original download available', async () => {
    globalThis.fetch = (async () => new Response('x'.repeat(500_001))) as typeof fetch
    act(() => root.render(createElement(AttachmentView, { id: 'one', label: 'large.md' })))
    await click('Read large.md')
    expect(win.document.querySelector('[role=alert]')?.textContent).toContain('too large')
    expect(win.document.querySelector('dialog a')?.getAttribute('href')).toBe('/api/attachments/one/download')
  })

  test('aborts a delayed oversized response as soon as headers arrive', async () => {
    let signal: AbortSignal | undefined
    let aborted = false
    globalThis.fetch = (async (_url, options) => {
      signal = options?.signal ?? undefined
      signal?.addEventListener('abort', () => {
        aborted = true
      })
      const body = new ReadableStream<Uint8Array>({ pull: () => new Promise(() => {}) })
      return new Response(body, { headers: { 'Content-Length': '500001' } })
    }) as typeof fetch
    act(() => root.render(createElement(AttachmentView, { id: 'one', label: 'large.md' })))
    await click('Read large.md')
    expect(win.document.querySelector('[role=alert]')?.textContent).toContain('too large')
    expect(signal?.aborted).toBe(true)
    expect(aborted).toBe(true)
    expect(win.document.querySelector('dialog')?.open).toBe(true)
  })

  test('does not replace reopened content with a late response', async () => {
    const pending = Promise.withResolvers<Response>()
    let calls = 0
    globalThis.fetch = (async () => (++calls === 1 ? pending.promise : new Response('# New'))) as typeof fetch
    act(() => root.render(createElement(AttachmentView, { id: 'one', label: 'notes.md' })))
    await click('Read notes.md')
    await click('Close reader')
    await click('Read notes.md')
    await act(async () => {
      pending.resolve(new Response('# Old'))
      await Bun.sleep(0)
    })
    expect(win.document.querySelector('dialog h1')?.textContent).toBe('New')
  })
})
