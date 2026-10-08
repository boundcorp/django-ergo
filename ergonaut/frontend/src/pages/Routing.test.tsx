import { describe, expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import type { Routing, RoutingProvider, RoutingWindow } from '../api'
import { Priorities, Providers, Tiers } from './Routing'

const reportedAt = '2026-10-07T10:00:00Z'

function window(overrides: Partial<RoutingWindow> = {}): RoutingWindow {
  return { used: 20, remaining: 80, resets_at: 1791453600, limit: 90, ...overrides }
}

function provider(overrides: Partial<RoutingProvider> = {}): RoutingProvider {
  return {
    name: 'codex',
    type: 'openai',
    transport: 'cli',
    subscription: true,
    api_key_env: '',
    status: 'standby',
    reason: '',
    windows: {},
    reported_at: reportedAt,
    stale: false,
    ...overrides,
  }
}

function routing(overrides: Partial<Routing> = {}): Routing {
  return {
    providers: [],
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
    default_max_used: 98,
    switches: [],
    editable: false,
    ...overrides,
  }
}

const renderProviders = (providers: RoutingProvider[]) => renderToStaticMarkup(createElement(Providers, { providers }))

describe('Routing pay-per-token providers', () => {
  test('lists only API-key providers, unmetered; subscriptions belong to the capacity section', () => {
    const html = renderProviders([
      provider({ name: 'subscription-provider' }),
      provider({
        name: 'api-provider',
        subscription: false,
        transport: 'api',
        api_key_env: 'OPENAI_API_KEY',
        status: 'api_key',
        reason: "OPENAI_API_KEY isn't set",
        reported_at: null,
      }),
    ])
    expect(html).toContain('Pay-per-token providers')
    expect(html).toContain('api-provider')
    expect(html).toContain('OPENAI_API_KEY')
    expect(html).toContain('Not metered by the router')
    expect(html).not.toContain('subscription-provider')
    expect(html).not.toContain('role="meter"')
  })

  test('renders nothing without API-key providers', () => {
    expect(renderProviders([provider()])).toBe('')
  })
})

describe('Routing tiers and rules', () => {
  test('shows configured custom chat and coding-agent tiers without fixed tier names', () => {
    const html = renderToStaticMarkup(
      createElement(Tiers, {
        data: routing({
          tiers: [{ name: 'research-plus', picked: 'codex/model', chats: 3, candidates: [] }],
          agents: [{ name: 'batch_jobs', candidates: [] }],
        }),
      }),
    )
    expect(html).toContain('research-plus')
    expect(html).toContain('batch_jobs')
    expect(html).toContain('3 chats')
    expect(html).not.toContain('>low<')
    expect(html).not.toContain('>medium<')
    expect(html).not.toContain('>high<')
  })

  test('labels Fable and arbitrary window rules accurately, preferring reported labels', () => {
    const html = renderToStaticMarkup(
      createElement(Priorities, {
        data: routing({
          providers: [provider({ windows: { daily_requests: window({ label: 'Daily request allowance' }) } })],
          rules: {
            limits: [
              { provider: '*', window: 'weekly_fable', max_used: 85 },
              { provider: 'codex', window: 'daily_requests', max_used: 80 },
              { provider: '*', window: 'burst_tokens', max_used: 70 },
            ],
          },
        }),
        onSaved: () => undefined,
      }),
    )
    expect(html).toContain('Weekly · Fable window')
    expect(html).toContain('Daily request allowance window')
    expect(html).toContain('Burst tokens window')
    expect(html).toContain('skip at 85%')
  })
})
