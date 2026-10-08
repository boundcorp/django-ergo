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

describe('Routing provider windows', () => {
  test('renders only the reported Codex 7-day window with its remaining, status, reset and limit', () => {
    const html = renderProviders([provider({ windows: { weekly: window({ label: '7-day', status: 'allowed' }) } })])
    expect(html.match(/role="meter"/g)?.length).toBe(1)
    expect(html).toContain('7-day')
    expect(html).toContain('80% remaining')
    expect(html).toContain('allowed')
    expect(html).toContain('resets ')
    expect(html).toContain('Skip at 90% used')
    expect(html).not.toContain('5-hour')
  })

  test('renders all three Claude windows including the model-scoped weekly window', () => {
    const html = renderProviders([
      provider({
        name: 'claude',
        type: 'anthropic',
        windows: {
          five_hour: window({ label: '5-hour' }),
          weekly: window({ label: 'Weekly' }),
          weekly_fable: window({ label: 'Weekly · Fable', model: 'fable', remaining: 42, used: 58 }),
        },
      }),
    ])
    expect(html.match(/role="meter"/g)?.length).toBe(3)
    expect(html).toContain('Weekly · Fable')
    expect(html).toContain('42% remaining')
    expect(html).toContain('fable only')
  })

  test('renders arbitrary reported windows and supports optional metadata', () => {
    const html = renderProviders([provider({ windows: { daily_requests: { used: 12, resets_at: null, limit: 80 } } })])
    expect(html).toContain('Daily requests')
    expect(html).toContain('88% remaining')
    expect(html).toContain('Skip at 80% used')
    expect(html.match(/role="meter"/g)?.length).toBe(1)
  })

  test('does not invent usage rows for a provider without a snapshot', () => {
    const html = renderProviders([provider({ windows: {}, reported_at: null })])
    expect(html).toContain('No usage reported yet')
    expect(html).not.toContain('role="meter"')
    expect(html).not.toContain('5-hour')
    expect(html).not.toContain('Weekly')
    expect(html).not.toContain('Last reported')
  })

  test('does not present unreported percentages as zero usage', () => {
    const html = renderProviders([
      provider({ windows: { weekly: window({ used: null, remaining: null, status: 'unknown' }) } }),
    ])
    expect(html).toContain('usage not reported')
    expect(html).toContain('unknown')
    expect(html).not.toContain('role="meter"')
  })

  test('keeps the report timestamp and stale indicator visible alongside provider reasons', () => {
    const html = renderProviders([provider({ reason: 'Weekly limit reached', stale: true, status: 'skipped' })])
    expect(html).toContain('Weekly limit reached')
    expect(html).toContain('Last reported')
    expect(html).toMatch(new RegExp(`datetime="${reportedAt}"`, 'i'))
    expect(html).toContain('Stale usage snapshot')
    expect(renderProviders([provider()])).not.toContain('Stale usage snapshot')
  })

  test('puts API keys in their own pay-per-token section, not under subscriptions', () => {
    const html = renderProviders([
      provider({ name: 'subscription-provider' }),
      provider({
        name: 'api-provider',
        subscription: false,
        transport: 'api',
        api_key_env: 'OPENAI_API_KEY',
        status: 'api_key',
        reported_at: null,
      }),
    ])
    const [subscriptionSection, apiSection] = html.split('<section aria-labelledby="routing-api-keys">')
    expect(subscriptionSection).toContain('subscription-provider')
    expect(subscriptionSection).not.toContain('api-provider')
    expect(apiSection).toContain('Pay-per-token providers')
    expect(apiSection).toContain('api-provider')
    expect(apiSection).toContain('OPENAI_API_KEY')
    expect(apiSection).toContain('Not metered by the router')
    expect(apiSection).not.toContain('No usage reported yet')
    expect(apiSection).not.toContain('role="meter"')
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
