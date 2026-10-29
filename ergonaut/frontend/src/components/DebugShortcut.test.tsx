import { afterEach, beforeEach, describe, expect, test } from 'bun:test'
import { act, StrictMode, type ComponentProps } from 'react'
import type { Root } from 'react-dom/client'
import { Window } from 'happy-dom'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { api } from '../api'
import { NewThread } from '../pages/NewThread'
import { DebugShortcut } from './DebugShortcut'

const id = '12345678-1234-1234-1234-123456789abc'
const source = `http://localhost/s/${id}`
let window: Window
let root: Root
let container: HTMLDivElement
const original = { debugShortcut: api.debugShortcut, models: api.models, newThread: api.newThread, send: api.send }

function button() {
  return [...container.querySelectorAll('button')].find(node => node.textContent === 'Debug') as HTMLButtonElement
}
function text() {
  return (container.querySelector('textarea[aria-label="First message"]') as HTMLTextAreaElement).value
}
function router(initialEntries: Parameters<typeof createMemoryRouter>[1] = { initialEntries: [`/s/${id}`] }) {
  return createMemoryRouter(
    [
      { path: '/s/:id', element: <DebugShortcut sessionId={id} /> },
      { path: '/bots/:name/new-thread', element: <NewThread onChange={() => undefined} /> },
    ],
    initialEntries,
  )
}
async function mount(r: ComponentProps<typeof RouterProvider>['router']) {
  await act(async () => {
    root.render(
      <StrictMode>
        <RouterProvider router={r} />
      </StrictMode>,
    )
    await Promise.resolve()
  })
}

beforeEach(async () => {
  window = new Window({ url: 'http://localhost' })
  Object.assign(globalThis, {
    window,
    document: window.document,
    navigator: window.navigator,
    HTMLElement: window.HTMLElement,
    Event: window.Event,
    localStorage: window.localStorage,
    IS_REACT_ACT_ENVIRONMENT: true,
  })
  api.models = async () => ({ models: [], default: '', default_engine_type: '' })
  // React DOM captures the document on first import, after Happy DOM is installed.
  const { createRoot } = await import('react-dom/client')
  container = window.document.createElement('div')
  window.document.body.append(container)
  root = createRoot(container)
})
afterEach(() => {
  act(() => root.unmount())
  Object.assign(api, original)
  window.happyDOM.close()
})

describe('debug shortcut', () => {
  test('gets an authorized link, preserves the draft, and consumes state across history navigation', async () => {
    window.localStorage.setItem('ergonaut.draft.new-thread.devbox', 'Existing draft')
    let requested = ''
    let sends = 0
    api.debugShortcut = async sessionId => {
      requested = sessionId
      return { url: `/s/${id}` }
    }
    api.newThread = async () => {
      sends++
      throw new Error('unexpected send')
    }
    api.send = async () => {
      sends++
      throw new Error('unexpected send')
    }
    const r = router()
    await mount(r)
    expect(button().type).toBe('button')
    await act(async () => {
      button().click()
      await Promise.resolve()
    })
    expect(requested).toBe(id)
    expect(r.state.location.pathname).toBe('/bots/devbox/new-thread')
    expect(text()).toBe(`Existing draft\n\n${source}`)
    expect(r.state.location.state).toBeNull()
    expect(sends).toBe(0)
    const textarea = container.querySelector('textarea') as HTMLTextAreaElement
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set
    if (!setter) throw new Error('Textarea value setter is unavailable')
    act(() => {
      setter.call(textarea, 'Edited draft')
      textarea.dispatchEvent(new window.Event('input', { bubbles: true }))
    })
    await act(async () => {
      await r.navigate(-1)
    })
    await act(async () => {
      await r.navigate(1)
    })
    expect(text()).toBe('Edited draft')
    expect(sends).toBe(0)
  })

  test('shows request errors and allows retry without navigating', async () => {
    let calls = 0
    api.debugShortcut = async () => {
      calls++
      if (calls === 1) throw new Error('Access denied')
      return { url: `/s/${id}` }
    }
    const r = router()
    await mount(r)
    await act(async () => {
      button().click()
      await Promise.resolve()
    })
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('Access denied')
    expect(r.state.location.pathname).toBe(`/s/${id}`)
    expect(button().disabled).toBe(false)
    await act(async () => {
      button().click()
      await Promise.resolve()
    })
    expect(r.state.location.pathname).toBe('/bots/devbox/new-thread')
    expect(text()).toBe(source)
  })

  test('rejects invalid server links and malformed navigation state', async () => {
    api.debugShortcut = async () => ({ url: 'https://example.com/s/secret' })
    const r = router()
    await mount(r)
    await act(async () => {
      button().click()
      await Promise.resolve()
    })
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('Invalid chat link')
    expect(r.state.location.pathname).toBe(`/s/${id}`)
    await act(async () => {
      await r.navigate('/bots/devbox/new-thread', {
        state: { debugSourceUrl: 'https://example.com/s/abcd', other: 'kept' },
      })
    })
    expect(text()).toBe('')
    expect(r.state.location.state).toEqual({ other: 'kept' })
  })

  test('each new navigation can add its link without replacing an existing draft', async () => {
    const r = router({ initialEntries: ['/bots/devbox/new-thread'] })
    await mount(r)
    await act(async () => {
      await r.navigate('/bots/devbox/new-thread', { state: { debugSourceUrl: source, other: 7 } })
    })
    expect(text()).toBe(source)
    expect(r.state.location.state).toEqual({ other: 7 })
    await act(async () => {
      await r.navigate('/bots/devbox/new-thread', { state: { debugSourceUrl: source } })
    })
    expect(text()).toBe(`${source}\n\n${source}`)
  })
})
