import { afterEach, beforeEach, describe, expect, test } from 'bun:test'
import { act, createElement } from 'react'
import type { createRoot as CreateRoot, Root } from 'react-dom/client'
import { Window } from 'happy-dom'
import { api } from '../api'
import type { SessionUsage, SessionUsageHistory } from '../api'
import { UsageHistory } from './UsageHistory'

let window: Window
let root: Root
let createRoot: typeof CreateRoot
let container: HTMLDivElement
const original = api.sessionUsageHistory
const usage: SessionUsage = {
  until: '2026-10-29T16:00:00Z',
  hours: [{ start: '2026-10-29T15:00:00Z', thread_tokens: 123, agent_tokens: 45 }],
  thread_tokens: 123,
  agent_tokens: 45,
  agent_usage_complete: false,
  agent_usage_coverage: {
    status: 'partial',
    excluded: ['orca', 'native_session', 'legacy'],
    collection: 'periodic',
    zero_is_conclusive: false,
  },
}
const history: SessionUsageHistory = {
  until: '2026-10-29T16:00:00Z',
  timezone: 'UTC',
  agent_usage_complete: false,
  agent_usage_coverage: {
    status: 'partial',
    excluded: ['orca', 'native_session', 'legacy'],
    collection: 'periodic',
    zero_is_conclusive: false,
  },
  days: [{ date: '2026-10-29', thread_tokens: 800, agent_tokens: 200 }],
  all_time: {
    thread: { input_tokens: 101, output_tokens: 102, cache_write_tokens: 103, cache_read_tokens: 104 },
    agents: { input_tokens: 201, output_tokens: 202, cache_write_tokens: 203, cache_read_tokens: 204 },
  },
  models: [{ model: 'example-model', thread_tokens: 900, agent_tokens: 300 }],
}

beforeEach(async () => {
  window = new Window()
  Object.assign(globalThis, { window, document: window.document, IS_REACT_ACT_ENVIRONMENT: true })
  window.HTMLDialogElement.prototype.showModal = function () {
    this.open = true
  }
  window.HTMLDialogElement.prototype.close = function () {
    this.open = false
  }
  // React DOM captures the document at import time, after Happy DOM is installed.
  ;({ createRoot } = await import('react-dom/client'))
  container = window.document.createElement('div') as unknown as HTMLDivElement
  window.document.body.appendChild(container as never)
  root = createRoot(container)
})

afterEach(() => {
  act(() => root.unmount())
  api.sessionUsageHistory = original
  window.close()
})

function render() {
  act(() => root.render(createElement(UsageHistory, { sessionId: 'thread-1', usage })))
}
function opener() {
  return container.querySelector('button') as HTMLButtonElement
}
function dialog() {
  return window.document.querySelector('dialog') as HTMLDialogElement
}

describe('UsageHistory', () => {
  test('opens from the sparkline, loads the selected thread and shows all breakdowns', async () => {
    let resolve!: (value: SessionUsageHistory) => void
    let requested = ''
    api.sessionUsageHistory = id => {
      requested = id
      return new Promise(r => {
        resolve = r
      })
    }
    render()
    opener().focus()
    act(() => opener().click())
    expect(dialog().open).toBe(true)
    expect(requested).toBe('thread-1')
    expect(dialog().textContent).toContain('Loading usage history')
    expect(dialog().textContent).toContain('Orca worker usage is unavailable')
    expect(dialog().textContent).toContain('recent usage may be delayed')
    expect(dialog().textContent).toContain('Zero recorded agent tokens does not mean no agent usage')
    expect(dialog().textContent).toContain('123')
    await act(async () => resolve(history))
    expect(dialog().textContent).toContain('2026-10-29')
    expect(dialog().textContent).toContain('204')
    expect(dialog().textContent).toContain('example-model')
    act(() => dialog().dispatchEvent(new window.Event('cancel', { cancelable: true }) as never))
    expect(dialog().open).toBe(false)
    expect(window.document.activeElement).toBe(opener())
  })

  test('chart bars expose exact values on focus and selection', async () => {
    api.sessionUsageHistory = async () => history
    render()
    await act(async () => opener().click())
    const hourly = dialog().querySelector(
      '[aria-label="Last hour: 123 thread tokens, 45 known agent tokens"]',
    ) as HTMLButtonElement
    expect(hourly).not.toBeNull()
    act(() => hourly.focus())
    expect(dialog().querySelector('[aria-live="polite"]')?.textContent).toContain('123 thread · 45 known agents')
    const daily = dialog().querySelector(
      '[aria-label="2026-10-29: 800 thread tokens, 200 known agent tokens"]',
    ) as HTMLButtonElement
    act(() => daily.click())
    expect(daily.closest('section')?.textContent).toContain('800 thread · 200 known agents')
  })

  test('offers retry and displays empty history', async () => {
    api.sessionUsageHistory = async () => {
      throw new Error('unavailable')
    }
    render()
    await act(async () => opener().click())
    expect(dialog().querySelector('[role="alert"]')?.textContent).toContain('Could not load')
    api.sessionUsageHistory = async () => ({ ...history, days: [], models: [] })
    const retry = [...dialog().querySelectorAll('button')].find(button => button.textContent === 'Try again')!
    await act(async () => retry.click())
    expect(dialog().textContent).toContain('No daily usage available')
    expect(dialog().textContent).toContain('No model usage available')
    act(() => (dialog().querySelector('[aria-label="Close usage history"]') as HTMLButtonElement).click())
    expect(dialog().open).toBe(false)
    expect(window.document.activeElement).toBe(opener())
  })

  test('closing the history does not close its parent mobile sheet', async () => {
    let parentClosed = false
    api.sessionUsageHistory = async () => history
    act(() =>
      root.render(
        createElement(
          'div',
          {
            onClose: () => {
              parentClosed = true
            },
          },
          createElement(UsageHistory, { sessionId: 'thread-1', usage }),
        ),
      ),
    )
    await act(async () => opener().click())
    act(() => dialog().dispatchEvent(new window.Event('close', { bubbles: true }) as never))
    expect(parentClosed).toBe(false)
  })
})
