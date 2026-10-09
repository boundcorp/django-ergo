import { describe, expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import type { Routing, RoutingProvider, RoutingWindow } from '../api'
import { countdown, pace, shortTag, tightest, tone, UsagePanel, UsageTrigger } from './UsageStatus'

const now = 1_800_000_000

function window(overrides: Partial<RoutingWindow> = {}): RoutingWindow {
  return { used: 20, remaining: 80, resets_at: now + 3600, limit: 90, ...overrides }
}

function provider(overrides: Partial<RoutingProvider> = {}): RoutingProvider {
  return {
    name: 'claude-max',
    type: 'claude',
    transport: 'cli',
    subscription: true,
    api_key_env: '',
    status: 'in_use',
    reason: '',
    windows: {},
    reported_at: new Date((now - 120) * 1000).toISOString(),
    stale: false,
    ...overrides,
  }
}

function routing(providers: RoutingProvider[]): Routing {
  return {
    providers,
    capacity: {
      sync: { state: 'empty', running: false, attempted_at: null, succeeded_at: null, error: '', stale_after: 900 },
      accounts: [],
    },
    tiers: [],
    agents: [],
    text: '',
    text_source: '',
    file_text: '',
    updated_at: null,
    compiled: false,
    compiling: false,
    compile_error: '',
    rules: { limits: [] },
    default_max_used: 90,
    switches: [],
    editable: false,
  }
}

describe('usage helpers', () => {
  test('pace is the share of the window already gone', () => {
    // 5-hour window resetting in 1 hour: 80% has passed.
    expect(pace('five_hour', window({ resets_at: now + 3600 }), now)).toBeCloseTo(80)
    expect(pace('weekly_fable', window({ resets_at: now + 7 * 86400 }), now)).toBeCloseTo(0)
    expect(pace('minutes_60', window({ resets_at: now + 1800 }), now)).toBeCloseTo(50)
    expect(pace('mystery', window(), now)).toBeNull()
    expect(pace('five_hour', window({ resets_at: null }), now)).toBeNull()
  })

  test('short tags and countdowns', () => {
    expect(shortTag('five_hour')).toBe('5h')
    expect(shortTag('weekly')).toBe('wk')
    expect(shortTag('weekly_fable', window({ model: 'fable' }))).toBe('Fable')
    expect(countdown(now + 38 * 60, now)).toBe('38m')
    expect(countdown(now + 2 * 3600 + 38 * 60, now)).toBe('2h 38m')
    expect(countdown(now + 3 * 86400 + 16 * 3600, now)).toBe('3d 16h')
  })

  test('tone follows the routing limit and the tightest window has least headroom', () => {
    expect(tone(window({ used: 50 }))).toBe('ok')
    expect(tone(window({ used: 80 }))).toBe('warn')
    expect(tone(window({ used: 95 }))).toBe('over')
    expect(tone(window({ used: null }))).toBe('none')
    const claude = provider({ windows: { five_hour: window({ used: 25 }), weekly: window({ used: 54 }) } })
    const codex = provider({ name: 'codex', type: 'openai', windows: { weekly: window({ used: 40, limit: 50 }) } })
    expect(tightest([claude, codex])).toMatchObject({ key: 'weekly', provider: codex })
  })
})

describe('UsageTrigger', () => {
  test('one ring per limit window, with value and tag', () => {
    const html = renderToStaticMarkup(
      createElement(UsageTrigger, {
        now,
        providers: [
          provider({ windows: { five_hour: window({ used: 25 }), weekly: window({ used: 54 }) } }),
          provider({ name: 'api', subscription: false, windows: { weekly: window() } }),
        ],
      }),
    )
    expect(html.match(/class="usage-ring /g)?.length).toBe(2)
    expect(html).toContain('>25%<')
    expect(html).toContain('>5h<')
    expect(html).toContain('>wk<')
    expect(html).toContain('usage-pace')
  })
})

describe('UsagePanel', () => {
  test('headline names the tightest limit and says what is missing', () => {
    const html = renderToStaticMarkup(
      createElement(
        MemoryRouter,
        null,
        createElement(UsagePanel, {
          now,
          data: routing([
            provider({
              windows: {
                five_hour: window({ used: null, remaining: null, status: 'reset' }),
                weekly: window({ used: 81, resets_at: now + 5 * 86400 }),
              },
            }),
            provider({ name: 'codex', type: 'openai', windows: {}, reported_at: null }),
            provider({ name: 'anthropic-api', subscription: false }),
          ]),
        }),
      ),
    )
    expect(html).toContain('81% of claude-max Weekly used.')
    expect(html).toContain('Reset · not reported since')
    expect(html).toContain('No usage reported yet')
    expect(html).toContain('Pay per token, no limits reported: anthropic-api.')
    expect(html).toContain('href="/routing"')
  })
})
