import { describe, expect, test } from 'bun:test'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import type { Bot, Session } from '../api'
import { DashboardWorkspace, DirectoryNotice } from '../components/DashboardWorkspace'
import { FullRow, ThreadGroups, groupOf } from '../components/ThreadList'
import { dashboardSessions } from './ThreadsPage'

function session(id: string, overrides: Partial<Session> = {}): Session {
  return {
    id,
    title: id,
    bot: 'helper',
    role: 'thread',
    status: 'active',
    parent_id: null,
    username: 'demo',
    created_at: '2026-10-12T10:00:00Z',
    updated_at: '2026-10-12T10:00:00Z',
    ...overrides,
  }
}
const bot: Bot = {
  name: 'helper',
  description: 'A helpful bot',
  orchestration: true,
  knowledge: false,
  parent: '',
  root_session_id: null,
}

describe('dashboard session scope', () => {
  test('keeps resolved threads but excludes closed named/main chats, without reordering results', () => {
    const rows = [
      session('resolved', { status: 'completed' }),
      session('closed-chat', { role: 'main', status: 'completed' }),
      session('open-chat', { role: 'chat' }),
      session('other', { bot: 'other' }),
    ]
    expect(dashboardSessions(rows, '').map(s => s.id)).toEqual(['resolved', 'open-chat', 'other'])
    expect(dashboardSessions(rows, 'helper').map(s => s.id)).toEqual(['resolved', 'open-chat'])
    expect(dashboardSessions(rows, 'missing')).toEqual([])
  })
  test('pin placement overrides attention while the attention state remains on the row', () => {
    const waiting = session('approval', { bucket: 'waiting', waiting_for: 'approval', pinned: true })
    expect(groupOf(waiting)).toBe('pinned')
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <FullRow session={waiting} />
      </MemoryRouter>,
    )
    expect(html).toContain('Approve or deny a tool call')
    expect(html).toContain('aria-label="Unpin approval"')
    expect(html).toContain('aria-pressed="true"')
  })
})

describe('dashboard presentation', () => {
  test('sorts within groups by last activity, falls back to update time, and does not mutate caller data', () => {
    const rows = [
      session('older', { bucket: 'waiting', updated_at: '2026-10-10T10:00:00Z' }),
      session('active-first', { bucket: 'waiting', last_activity: '2026-10-13T10:00:00Z' }),
      session('pinned', { pinned: true }),
    ]
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <ThreadGroups sessions={rows} scope="dashboard-test" full />
      </MemoryRouter>,
    )
    expect(html.indexOf('href="/s/pinned"')).toBeLessThan(html.indexOf('href="/s/active-first"'))
    expect(html.indexOf('href="/s/active-first"')).toBeLessThan(html.indexOf('href="/s/older"'))
    expect(rows.map(s => s.id)).toEqual(['older', 'active-first', 'pinned'])
  })
  test('presents actionable PR navigation, worker progress and unread/busy semantics together', () => {
    const row = session('working', {
      unread: true,
      busy: true,
      workers_running: 1,
      workers_total: 3,
      prs: [
        {
          id: 'pr',
          url: 'https://github.com/example/project/pull/8',
          repo: 'example/project',
          number: 8,
          title: 'Fix',
          state: 'open',
          checks: 'failing',
        },
      ],
    })
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <FullRow session={row} />
      </MemoryRouter>,
    )
    expect(html).toContain('href="https://github.com/example/project/pull/8"')
    expect(html).toContain('checks failing')
    expect(html).toContain('1 running · 2/3 workers done')
    expect(html).toContain('Unread reply')
    expect(html).toContain('Working')
  })
  test('pin shortcuts keep the owning chat and safely encode page names and URLs', () => {
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <DashboardWorkspace
          bots={[bot]}
          sessions={[session('owner')]}
          pins={{ owner: [{ name: 'Plan & budget', url: '/pages/demo?year=2026&view=all' }] }}
        />
      </MemoryRouter>,
    )
    expect(html).toContain(
      'href="/s/owner?pin=%2Fpages%2Fdemo%3Fyear%3D2026%26view%3Dall&amp;name=Plan%20%26%20budget"',
    )
    expect(html).toContain('href="/bots/helper/new-thread"')
  })
  test('loading and failed refreshes expose status and retry instead of empty-state claims', () => {
    const loading = renderToStaticMarkup(<DirectoryNotice state="loading" onRetry={() => {}} />)
    const failure = renderToStaticMarkup(<DirectoryNotice state="error" onRetry={() => {}} />)
    expect(loading).toContain('role="status"')
    expect(loading).not.toContain('<button')
    expect(failure).toContain('role="alert"')
    expect(failure).toContain('<button')
    expect(failure).not.toContain('No threads')
  })
})
