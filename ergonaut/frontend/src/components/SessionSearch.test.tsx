import { afterEach, beforeEach, describe, expect, test } from 'bun:test'
import { act } from 'react'
import type { Root } from 'react-dom/client'
import { Window } from 'happy-dom'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { api, type SessionSearchResult } from '../api'
import { SessionSearch } from './SessionSearch'

let window: Window
let root: Root
let container: HTMLDivElement
const originalSearch = api.searchSessions
const row = (id: string, title: string, line: number | null = null): SessionSearchResult => ({
  id,
  title,
  bot: 'helper',
  resolved: false,
  last_activity: null,
  snippet: 'A <script> search result',
  line,
})
function dialog() {
  return window.document.querySelector('[role="dialog"]') as HTMLElement
}
function input() {
  return dialog().querySelector('input') as HTMLInputElement
}
function type(value: string) {
  const element = input()
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')!.set!
  act(() => {
    setter.call(element, value)
    element.dispatchEvent(new window.Event('input', { bubbles: true }))
  })
}
async function debounce() {
  await act(async () => {
    await new Promise(resolve => setTimeout(resolve, 280))
  })
}
function key(key: string, options: Record<string, boolean> = {}) {
  act(() => input().dispatchEvent(new window.KeyboardEvent('keydown', { key, bubbles: true, ...options })))
}

beforeEach(async () => {
  window = new Window({ url: 'http://localhost' })
  Object.assign(globalThis, {
    window,
    document: window.document,
    navigator: window.navigator,
    HTMLElement: window.HTMLElement,
    Event: window.Event,
    IS_REACT_ACT_ENVIRONMENT: true,
  })
  // React DOM captures the document at initialization, after Happy DOM is installed.
  const { createRoot } = await import('react-dom/client')
  container = window.document.createElement('div')
  window.document.body.append(container)
  root = createRoot(container)
})
afterEach(() => {
  act(() => root.unmount())
  api.searchSessions = originalSearch
  window.happyDOM.close()
})

describe('conversation search', () => {
  test('opens by shortcut, debounces, highlights safely, navigates with keyboard and restores focus', async () => {
    const calls: string[] = []
    api.searchSessions = async q => {
      calls.push(q)
      return [row('one', 'Search title'), { ...row('two', 'Other', 42), resolved: true }]
    }
    const router = createMemoryRouter([{ path: '*', element: <SessionSearch /> }], { initialEntries: ['/'] })
    act(() => root.render(<RouterProvider router={router} />))
    const trigger = container.querySelector('button')!
    trigger.focus()
    act(() =>
      window.dispatchEvent(
        new window.KeyboardEvent('keydown', { key: 'k', ctrlKey: true, bubbles: true, cancelable: true }),
      ),
    )
    expect(window.document.activeElement).toBe(input())
    type('sea')
    type('search')
    expect(dialog().textContent).toContain('Searching')
    await debounce()
    expect(calls).toEqual(['search'])
    expect(dialog().querySelector('mark')?.textContent).toBe('Search')
    expect(dialog().textContent).toContain('Active')
    expect(dialog().textContent).toContain('Resolved')
    expect(dialog().querySelector('script')).toBeNull()
    key('ArrowDown')
    key('Enter')
    expect(router.state.location.pathname).toBe('/s/two')
    expect(router.state.location.hash).toBe('#m-42')
    expect(window.document.querySelector('[role="dialog"]')).toBeNull()
    expect(window.document.activeElement).toBe(trigger)
  })

  test('discards stale requests, handles empty and error states, and Escape does not reach drawer', async () => {
    let resolveOld!: (rows: SessionSearchResult[]) => void
    api.searchSessions = q =>
      q === 'old'
        ? new Promise(resolve => {
            resolveOld = resolve
          })
        : q === 'error'
          ? Promise.reject(new Error('offline'))
          : Promise.resolve([])
    const router = createMemoryRouter([{ path: '*', element: <SessionSearch /> }])
    act(() => root.render(<RouterProvider router={router} />))
    act(() => container.querySelector('button')!.click())
    type('old')
    await debounce()
    type('new')
    await debounce()
    expect(dialog().textContent).toContain('No conversations found')
    await act(async () => {
      resolveOld([row('old', 'Stale')])
      await Promise.resolve()
    })
    expect(dialog().textContent).not.toContain('Stale')
    type('error')
    await debounce()
    expect(dialog().textContent).toContain('Search is unavailable')
    let drawerEscapes = 0
    window.document.addEventListener('keydown', event => {
      if (event.key === 'Escape') drawerEscapes++
    })
    key('Escape')
    expect(drawerEscapes).toBe(0)
    expect(window.document.querySelector('[role="dialog"]')).toBeNull()
  })
})
