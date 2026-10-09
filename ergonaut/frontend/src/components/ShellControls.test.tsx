import { describe, expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import type { User, Worker } from '../api'
import { AccountMenu, nextMenuIndex, WorkerStatus } from './ShellControls'

const user: User = { id: 'user-1', username: 'lee', email: 'lee@example.com', first_name: 'Lee', last_name: '' }

function worker(overrides: Partial<Worker> = {}): Worker {
  return {
    id: 'worker-1',
    session_id: 'session-1',
    title: 'Review account menu',
    function: 'review',
    status: 'running',
    progress: '3 of 5 checks',
    result: null,
    error: '',
    created_at: '2026-10-08T10:00:00Z',
    completed_at: null,
    ...overrides,
  }
}

function accountMenu() {
  return renderToStaticMarkup(
    createElement(
      MemoryRouter,
      null,
      createElement(AccountMenu, { user, initialOpen: true, onSignOut: () => undefined }),
    ),
  )
}

describe('AccountMenu', () => {
  test('moves every former sidebar destination into the account menu', () => {
    const html = accountMenu()
    expect(html).toContain('role="menu"')
    expect(html).toContain('href="/sessions"')
    expect(html).toContain('href="/costs"')
    expect(html).toContain('href="/routing"')
    expect(html).toContain('href="/api-keys"')
    expect(html).toContain('>Sign out<')
    expect(html).toContain('aria-expanded="true"')
  })

  test('uses menu keyboard order for arrows, Home, and End', () => {
    expect(nextMenuIndex('ArrowDown', 3, 4)).toBe(0)
    expect(nextMenuIndex('ArrowUp', 0, 4)).toBe(3)
    expect(nextMenuIndex('Home', 2, 4)).toBe(0)
    expect(nextMenuIndex('End', 0, 4)).toBe(3)
    expect(nextMenuIndex('Enter', 1, 4)).toBeNull()
  })
})

describe('WorkerStatus', () => {
  test('distinguishes running and queued work and only shows reported progress', () => {
    const html = renderToStaticMarkup(
      createElement(WorkerStatus, {
        initialOpen: true,
        data: {
          state: 'ready',
          workers: [worker(), worker({ id: 'worker-2', title: 'Queued report', status: 'queued', progress: '' })],
        },
        onRetry: () => undefined,
      }),
    )
    expect(html).toContain('Working · 2 active')
    expect(html).toContain('Running')
    expect(html).toContain('Queued')
    expect(html).toContain('3 of 5 checks')
    expect(html).toContain('1 running, 1 queued')
  })

  test('keeps inactive, loading, and unavailable status truthful', () => {
    expect(
      renderToStaticMarkup(
        createElement(WorkerStatus, { data: { state: 'ready', workers: [] }, onRetry: () => undefined }),
      ),
    ).toBe('')
    expect(
      renderToStaticMarkup(
        createElement(WorkerStatus, { data: { state: 'loading', workers: [] }, onRetry: () => undefined }),
      ),
    ).toContain('Checking active work')
    expect(
      renderToStaticMarkup(
        createElement(WorkerStatus, {
          initialOpen: true,
          data: { state: 'error', workers: [] },
          onRetry: () => undefined,
        }),
      ),
    ).toContain('Retry')
  })
})

test('does not invent a subscription capacity summary without a capacity source', () => {
  const html = accountMenu()
  expect(html).toContain('href="/costs"')
  expect(html).not.toContain('remaining')
  expect(html).not.toContain('capacity')
})
