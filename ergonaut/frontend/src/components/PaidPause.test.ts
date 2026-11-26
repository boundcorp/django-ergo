import { describe, expect, test } from 'bun:test'
import type { PoolSettings } from '../api'
import { changePaidPause } from './Pools'

const state = (extra: Partial<PoolSettings> = {}): PoolSettings => ({
  legacy: false,
  pools: [],
  active: '',
  mode: 'auto',
  paid_paused: true,
  paid_pause_version: 'version-a',
  ...extra,
})

describe('paid dispatch pause', () => {
  test('resume requires confirmation and sends the observed version', async () => {
    const calls: unknown[][] = []
    const send = async (paused: boolean, version: string) => {
      calls.push([paused, version])
      return state()
    }
    expect(changePaidPause(state(), () => false, send)).toBeNull()
    expect(calls).toEqual([])
    await changePaidPause(state(), () => true, send)
    expect(calls).toEqual([[false, 'version-a']])
  })
  test('missing version refuses and stale errors propagate to the UI', async () => {
    const send = async () => {
      throw new Error('Paid dispatch state changed; refresh and try again')
    }
    expect(() => changePaidPause(state({ paid_pause_version: undefined }), () => true, send)).toThrow('refresh')
    await expect(changePaidPause(state(), () => true, send)).rejects.toThrow('state changed')
  })
})
