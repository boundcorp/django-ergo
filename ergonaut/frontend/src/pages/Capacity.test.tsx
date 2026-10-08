import { describe, expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import type { Capacity, CapacityAccount, CapacitySync, CapacityWindow } from '../api'
import { CapacityLoading, CapacitySection, countdown, rowsFor } from './Capacity'

const NOW = Date.parse('2026-10-08T20:00:00Z')
const seconds = (iso: string) => Date.parse(iso) / 1000

function win(overrides: Partial<CapacityWindow> = {}): CapacityWindow {
  return {
    key: 'five_hour',
    label: 'Claude 5 Hour',
    period: '5h',
    used: 5,
    remaining: 95,
    resets_at: seconds('2026-10-09T00:40:00Z'),
    status: 'ok',
    model: '',
    observed_at: NOW / 1000,
    stale: false,
    limit: null,
    ...overrides,
  }
}

const weekly = (overrides: Partial<CapacityWindow> = {}) =>
  win({
    key: 'weekly',
    label: 'Claude 7 Day',
    period: '7d',
    used: 52,
    remaining: 48,
    resets_at: seconds('2026-10-12T15:00:00Z'),
    ...overrides,
  })

function account(overrides: Partial<CapacityAccount> = {}): CapacityAccount {
  return {
    id: 'anthropic',
    name: 'Anthropic · Claude',
    providers: ['claude'],
    status: 'ok',
    error: '',
    fetched_at: '2026-10-08T19:58:00+00:00',
    windows: [win(), weekly()],
    ...overrides,
  }
}

function sync(overrides: Partial<CapacitySync> = {}): CapacitySync {
  return {
    state: 'healthy',
    running: false,
    attempted_at: '2026-10-08T19:58:00+00:00',
    succeeded_at: '2026-10-08T19:58:00+00:00',
    error: '',
    stale_after: 900,
    ...overrides,
  }
}

function render(capacity: Partial<Capacity> = {}, props: { refreshing?: boolean; refreshError?: string } = {}) {
  return renderToStaticMarkup(
    createElement(CapacitySection, {
      capacity: { sync: sync(), accounts: [account()], ...capacity },
      refreshing: false,
      refreshError: '',
      onRefresh: () => {},
      now: NOW,
      ...props,
    }),
  )
}

describe('reset countdown', () => {
  test('counts down in the largest two units', () => {
    const at = (iso: string) => countdown(seconds(iso), NOW)
    expect(at('2026-10-08T20:00:20Z')).toBe('under a minute')
    expect(at('2026-10-08T20:45:00Z')).toBe('45m')
    expect(at('2026-10-08T22:14:00Z')).toBe('2h 14m')
    expect(at('2026-10-12T04:00:00Z')).toBe('3d 08h')
    expect(at('2026-10-08T19:00:00Z')).toBe('under a minute') // already past: never negative
  })
})

describe('window rows', () => {
  test('shows 5-hour and 7-day usage with reset countdown and a meter for each', () => {
    const html = render()
    expect(html).toContain('5-hour window')
    expect(html).toContain('5% used')
    expect(html).toContain('95% left')
    expect(html).toContain('Resets in 4h 40m')
    expect(html).toContain('7-day window')
    expect(html).toContain('52% used')
    expect(html).toContain('Resets in 3d 19h')
    expect(html.match(/role="meter"/g)?.length).toBe(2)
    expect(html).toContain('aria-valuenow="52"')
  })

  test('a window the provider does not report is Unavailable, never 0%', () => {
    const codex = account({
      id: 'openai-codex',
      name: 'OpenAI · Codex',
      windows: [weekly({ used: 13, remaining: 87 })],
    })
    const html = render({ accounts: [codex] })
    expect(html).toContain('5-hour window')
    expect(html).toContain('Unavailable')
    expect(html).toContain('Not reported by provider')
    expect(html).toContain('13% used')
    expect(html.match(/role="meter"/g)?.length).toBe(1)
    expect(html).not.toContain('0% used')
  })

  test('a reported window without a value is unavailable, while a real 0% is still shown as 0%', () => {
    const blank = render({
      accounts: [account({ windows: [win({ used: null, remaining: null }), weekly({ used: 0, remaining: 100 })] })],
    })
    expect(blank).toContain('Unavailable')
    expect(blank).toContain('0% used')
    expect(blank).not.toContain('NaN')
    expect(blank.match(/role="meter"/g)?.length).toBe(1)
  })

  test('an account with no data lists both windows as unavailable and says why', () => {
    const html = render({ accounts: [account({ status: 'unavailable', windows: [], fetched_at: null })] })
    expect(html.match(/— · <\/span>Unavailable/g)?.length).toBe(2)
    expect(html).toContain('No data yet')
    expect(html).toContain('No data')
    expect(html).not.toContain('% used')
  })

  test('a window past its reset has no value until the next sync', () => {
    const html = render({
      accounts: [account({ windows: [win({ used: null, remaining: null, status: 'reset' }), weekly()] })],
    })
    expect(html).toContain('Window reset, waiting for a fresh sync')
  })

  test('shows every scoped window (model- or product-specific) under its own label', () => {
    const fable = weekly({
      key: 'weekly_fable',
      label: 'Claude 7 Day (Fable)',
      model: 'fable',
      used: 0,
      remaining: 100,
    })
    const html = render({ accounts: [account({ windows: [win(), weekly(), fable] })] })
    expect(html).toContain('Claude 7 Day (Fable)')
    expect(html).toContain('fable only')
    const grok = account({
      id: 'xai-oauth',
      name: 'xAI · Grok',
      providers: [],
      windows: [
        weekly({ key: 'weekly_credits', label: 'SuperGrok Weekly Credits', used: 32, remaining: 68 }),
        weekly({ key: 'weekly_grokbuild', label: 'Grok Build (Weekly)', used: 32, remaining: 68 }),
      ],
    })
    expect(rowsFor(grok).map(r => r.label)).toEqual([
      '5-hour window',
      'SuperGrok Weekly Credits',
      'Grok Build (Weekly)',
    ])
    expect(render({ accounts: [grok] })).toContain('Shown for reference, not used for routing')
  })

  test('marks windows near or over the router limit in words and shows where the router skips', () => {
    const html = render({
      accounts: [
        account({
          windows: [win({ used: 90, remaining: 10, limit: 85 }), weekly({ used: 72, remaining: 28, limit: 85 })],
        }),
      ],
    })
    expect(html).toContain('Over the router limit')
    expect(html).toContain('Near the router limit')
    expect(html).toContain('Router skips at 85% used')
  })
})

describe('stale and failed data', () => {
  test('stale values keep their number but carry a Stale badge and when they were observed', () => {
    const old = '2026-10-08T14:00:00.000Z'
    const html = render({
      accounts: [
        account({ status: 'stale', windows: [win(), weekly({ stale: true, observed_at: Date.parse(old) / 1000 })] }),
      ],
    })
    expect(html).toContain('52% used')
    expect(html).toContain('Stale')
    expect(html).toContain('as of')
    expect(html).toContain('6 hours ago')
    expect(html).toContain(`dateTime="${old}"`)
    expect(html).toContain(', stale"')
  })

  test('a failed provider shows why and that its numbers are its last known ones', () => {
    const html = render({
      sync: sync({ state: 'partial' }),
      accounts: [
        account({ status: 'error', error: 'token expired', windows: [win({ stale: true }), weekly({ stale: true })] }),
      ],
    })
    expect(html).toContain('Sync failed')
    expect(html).toContain('token expired')
    expect(html).toContain('last known')
    expect(html).toContain('Some providers failed to sync')
  })

  test.each([
    ['healthy', 'Source sync healthy'],
    ['stale', 'Limits are stale'],
    ['partial', 'Some providers failed to sync'],
    ['failed', 'Source sync failed'],
    ['empty', 'No limits synced yet'],
  ] as const)('sync state %s is announced in words', (state, title) => {
    const html = render({ sync: sync({ state, error: state === 'failed' ? 'omp isn’t installed' : '' }) })
    expect(html).toContain(title)
  })

  test('a failed sync shows its error, the last successful time and stays out of the healthy look', () => {
    const html = render({ sync: sync({ state: 'failed', error: 'omp isn’t installed' }) })
    expect(html).toContain('Last attempt failed: omp isn’t installed')
    expect(html).toContain('Last successful sync')
    expect(html).toContain('19:58 UTC')
    expect(html).not.toContain('Source sync healthy')
  })

  test('no successful sync yet says so instead of inventing a time', () => {
    const html = render({ sync: sync({ state: 'failed', succeeded_at: null, error: 'boom' }), accounts: [] })
    expect(html).toContain('No successful sync yet')
    expect(html).not.toContain('Last successful sync')
  })

  test('stale sync explains the threshold', () => {
    expect(render({ sync: sync({ state: 'stale' }) })).toContain('older than 15 minutes')
  })
})

describe('refresh and states', () => {
  test('the refresh button is a named, keyboard-operable button', () => {
    const html = render()
    expect(html).toMatch(/<button type="button"[^>]*aria-disabled="false"[^>]*>Refresh limits/)
  })

  test('refreshing shows progress, disables repeat clicks and still keeps the old values on screen', () => {
    const html = render({}, { refreshing: true })
    expect(html).toContain('Refreshing…')
    expect(html).toContain('Syncing limits…')
    expect(html).toContain('aria-disabled="true"')
    expect(html).toContain('52% used')
  })

  test('a sync already running elsewhere shows as in progress too', () => {
    expect(render({ sync: sync({ running: true }) })).toContain('Syncing limits…')
  })

  test('a failed refresh request is reported', () => {
    expect(render({}, { refreshError: 'Bad gateway' })).toContain('Refresh failed: Bad gateway')
  })

  test('nothing configured or synced explains how to get data', () => {
    const html = render({ sync: sync({ state: 'empty', succeeded_at: null }), accounts: [] })
    expect(html).toContain('No subscription providers are configured')
    expect(html).toContain('Refresh limits')
  })

  test('loading shows a busy region, not numbers', () => {
    const html = renderToStaticMarkup(createElement(CapacityLoading))
    expect(html).toContain('aria-busy="true"')
    expect(html).toContain('Loading limits…')
    expect(html).not.toContain('% used')
  })
})

describe('structure', () => {
  test('headings, live region and the provider-reported note are present', () => {
    const html = render()
    expect(html).toMatch(/<h2[^>]*>.*Source sync healthy/)
    expect(html).toContain('aria-live="polite"')
    expect(html).toContain('<h2 id="capacity-windows"')
    expect(html).toContain('<h3 id="capacity-anthropic"')
    expect(html).toContain('aria-labelledby="capacity-anthropic"')
    expect(html).toContain('Usage percentages are provider-reported')
    expect(html).toContain('grid gap-4 md:grid-cols-2')
  })

  test('each status badge has text, not only a color', () => {
    for (const [status, text] of [
      ['ok', 'Connected'],
      ['stale', 'Stale'],
      ['error', 'Sync failed'],
      ['unavailable', 'No data'],
    ] as const) {
      expect(render({ accounts: [account({ status })] })).toContain(text)
    }
  })
})
