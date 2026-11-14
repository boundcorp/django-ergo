import { afterEach, beforeEach, describe, expect, test } from 'bun:test'
import { act, createElement } from 'react'
import type { Root } from 'react-dom/client'
import { Window } from 'happy-dom'
import { api, ApiError, type RecurringWake } from '../api'
import { RecurringWakeNotice } from './RecurringWakeNotice'

const schedule: RecurringWake = {
  id: 'schedule-1',
  note: 'Check the oven',
  status: 'active',
  interval_seconds: 300,
  next_run_at: '2026-11-13T15:05:00Z',
  expires_at: '2026-11-13T17:00:00Z',
  max_occurrences: 12,
  occurrences: 2,
  periodic_checkins: false,
  blocked_reason: '',
}
let window: Window
let root: Root
let container: HTMLDivElement
const originalControl = api.controlRecurringWake

beforeEach(async () => {
  window = new Window({ url: 'http://localhost' })
  Object.assign(globalThis, {
    window,
    document: window.document,
    navigator: window.navigator,
    HTMLElement: window.HTMLElement,
    Event: window.Event,
    MouseEvent: window.MouseEvent,
    IS_REACT_ACT_ENVIRONMENT: true,
  })
  // React DOM captures document during module initialization; install the test DOM before loading it.
  const { createRoot } = await import('react-dom/client')
  container = window.document.createElement('div')
  window.document.body.append(container)
  root = createRoot(container)
})
afterEach(() => {
  act(() => root.unmount())
  api.controlRecurringWake = originalControl
  window.happyDOM.close()
})

function render(schedules: RecurringWake[] = [schedule]) {
  act(() => root.render(createElement(RecurringWakeNotice, { sessionId: 'thread-1', schedules, onChange: () => {} })))
}
function button(action: string): HTMLButtonElement {
  const found = container.querySelector<HTMLButtonElement>(
    `button[aria-label="${action} recurring wake: Check the oven"]`,
  )
  if (!found) throw new Error(`Missing ${action} button`)
  return found
}
async function click(action: string) {
  await act(async () => {
    button(action).click()
    await Promise.resolve()
  })
}

describe('RecurringWakeNotice', () => {
  test('blocked and paused schedules offer resume rather than another pause', () => {
    for (const status of ['blocked', 'paused'] as const) {
      render([{ ...schedule, status, blocked_reason: status === 'blocked' ? 'approval required' : '' }])
      expect(button('resume').disabled).toBe(false)
      expect(container.querySelector('button[aria-label^="pause recurring wake"]')).toBeNull()
      expect(button('cancel').disabled).toBe(false)
    }
  })

  test('completed and cancelled schedules cannot be resumed or cancelled again', () => {
    for (const status of ['completed', 'cancelled'] as const) {
      render([{ ...schedule, status }])
      expect(container.querySelector('button')).toBeNull()
    }
  })

  test('shows server errors and keeps the existing status instead of faking success', async () => {
    api.controlRecurringWake = async () => {
      throw new ApiError(409, 'This schedule has expired')
    }
    render()
    await click('pause')
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('This schedule has expired')
    expect(container.textContent).toContain('active')
    expect(button('pause').disabled).toBe(false)
  })

  test('disables duplicate controls while a request is pending', async () => {
    const { promise, resolve } = Promise.withResolvers<RecurringWake>()
    let requests = 0
    api.controlRecurringWake = () => {
      requests++
      return promise
    }
    render()
    act(() => {
      button('pause').click()
      button('pause').click()
    })
    expect(requests).toBe(1)
    expect(button('pause').disabled).toBe(true)
    expect(button('cancel').disabled).toBe(true)
    await act(async () => {
      resolve({ ...schedule, status: 'paused' })
      await Promise.resolve()
    })
    expect(button('pause').disabled).toBe(false)
  })
})
