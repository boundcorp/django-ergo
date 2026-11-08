import { describe, expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { WakeNotice, untilDue } from './WakeNotice'

const now = Date.parse('2026-11-07T15:00:00Z')

describe('WakeNotice', () => {
  test('shows the pending wake, how soon and its note', () => {
    const wake = {
      id: 'w1',
      due_at: '2026-11-07T15:12:00Z',
      note: 'Retry the Meta upload',
      chain: 1,
      status: 'pending',
    }
    const html = renderToStaticMarkup(createElement(WakeNotice, { wakes: [wake], now }))
    expect(html).toContain('Continues on its own at')
    expect(html).toContain('(in 12 min)')
    expect(html).toContain('Retry the Meta upload')
  })

  test('shows nothing without a pending wake', () => {
    expect(renderToStaticMarkup(createElement(WakeNotice, { wakes: [] }))).toBe('')
  })

  test('says how long until it is due', () => {
    expect(untilDue('2026-11-07T14:59:00Z', now)).toBe('now')
    expect(untilDue('2026-11-07T15:59:00Z', now)).toBe('in 59 min')
    expect(untilDue('2026-11-07T18:00:00Z', now)).toBe('in 3 h')
  })
})
