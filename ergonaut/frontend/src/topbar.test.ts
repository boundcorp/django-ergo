import { describe, expect, test } from 'bun:test'
import type { Session } from './api'
import { topbarLocation } from './topbar'

function session(overrides: Partial<Session> = {}): Session {
  return {
    id: 's1',
    bot: 'kitchen',
    title: 'Plan the week',
    role: 'thread',
    parent_id: null,
    status: 'active',
    username: 'lee',
    created_at: '2026-10-08T10:00:00Z',
    updated_at: '2026-10-08T10:00:00Z',
    ...overrides,
  }
}

describe('topbarLocation', () => {
  test('home is just the workspace, with no repeated crumb', () => {
    expect(topbarLocation('/')).toEqual({ crumbs: [], title: 'Workspace' })
  })

  test('a chat names its bot and parent thread', () => {
    const parent = session({ id: 'p1', title: 'Dinner menus' })
    const child = session({ id: 'c1', title: 'Shopping list', parent_id: 'p1' })
    expect(topbarLocation('/s/c1', [parent, child])).toEqual({
      crumbs: [
        { label: 'kitchen', to: '/bots/kitchen' },
        { label: 'Dinner menus', to: '/s/p1' },
      ],
      title: 'Shopping list',
    })
    expect(topbarLocation('/s/unknown', [parent]).title).toBe('Conversation')
  })

  test('bot pages and account pages', () => {
    expect(topbarLocation('/bots/kitchen')).toEqual({ crumbs: [{ label: 'Bots' }], title: 'kitchen' })
    expect(topbarLocation('/bots/kitchen/kb').title).toBe('Memory')
    expect(topbarLocation('/bots/kitchen/new-thread').crumbs[0].label).toBe('kitchen')
    expect(topbarLocation('/costs').title).toBe('Costs & usage')
  })
})
