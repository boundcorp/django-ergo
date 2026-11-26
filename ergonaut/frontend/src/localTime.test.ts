import { describe, expect, test } from 'bun:test'
import { fromLocalInput, toLocalInput } from './localTime'

describe('datetime-local conversion', () => {
  test('round-trips an instant without shifting it by the UTC offset', () => {
    const iso = '2026-12-31T23:30:00.000Z'
    expect(fromLocalInput(toLocalInput(iso))).toBe(iso)
  })

  test('shows the instant in local time, not the UTC wall clock', () => {
    const iso = '2026-07-01T12:00:00.000Z'
    const local = new Date(iso)
    const shown = toLocalInput(iso)
    expect(shown.slice(11, 13)).toBe(String(local.getHours()).padStart(2, '0'))
    expect(new Date(shown).getTime()).toBe(local.getTime())
  })

  test('empty and invalid values stay empty', () => {
    expect(toLocalInput(null)).toBe('')
    expect(toLocalInput('nope')).toBe('')
    expect(fromLocalInput('')).toBe('')
  })
})
