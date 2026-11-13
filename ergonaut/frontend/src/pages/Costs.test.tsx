import { describe, expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import type { AgentUsage, CostBucket, Costs, EstimateScope } from '../api'
import { CostsBody, Estimate, Summary, itemsFor, projectItems } from './Costs'

const bucket = (name: string, calls = 1): CostBucket => ({
  name,
  calls,
  input_tokens: 100,
  cache_write_tokens: 0,
  cache_read_tokens: 300,
  output_tokens: 50,
  reasoning_tokens: 0,
  input_cost: 0.1,
  cache_write_cost: 0,
  cache_read_cost: 0.03,
  output_cost: 0.2,
  cost: 0.33,
  unpriced_calls: 0,
  subscription_calls: 0,
})

const agent = (title: string, project: string, tokens: number): AgentUsage => ({
  worker_id: title,
  worker_title: title,
  agent: 'codex',
  model: 'gpt-6-sol',
  chat_id: 'chat',
  chat_title: 'devbox main',
  bot: 'devbox',
  worker_status: 'completed',
  input_tokens: tokens,
  cache_write_tokens: 0,
  cache_read_tokens: 0,
  output_tokens: 0,
  reasoning_tokens: 0,
  tokens,
  cache_hit: 0,
  requests: 1,
  project_key: project ? `remote:${project}` : '',
  project,
  project_remote: project ? `ssh://github.com/boundcorp/${project}` : '',
  branch: 'main',
  cost: tokens / 1e6,
  priced: true,
})

const rows = [agent('Fix verses', 'holyword', 900), agent('Timer', 'meditate', 300), agent('Search', 'holyword', 100)]
const project = (name: string, workers: number, tokens: number) => ({
  key: `remote:${name}`,
  name,
  remote: `ssh://github.com/boundcorp/${name}`,
  workers,
  agents: ['codex'],
  models: ['gpt-6-sol'],
  input_tokens: tokens,
  cache_write_tokens: 0,
  cache_read_tokens: 0,
  output_tokens: 0,
  tokens,
  cache_hit: 0,
  requests: workers,
  cost: tokens / 1e6,
  unpriced_models: [],
  last_at: null,
})

const data: Costs = {
  days: 7,
  total: bucket('total', 3),
  by_kind: [bucket('chat_reply', 2), bucket('compaction')],
  chat_reply_by_bot: [bucket('devbox', 2)],
  by_model: [bucket('gpt-6-sol', 3)],
  by_day: [{ date: '2026-10-12', cost: 0.33, calls: 3 }],
  unpriced_models: [],
  usage: {
    headline: {
      threads: 0,
      tokens: 450,
      cache_hit: 0.75,
      main_chats: 1,
      subscription: 0,
      api_spend: 0.33,
      compaction: 0,
    },
    threads: [],
  },
  agents: {
    headline: { sessions: 3, tokens: 1300, cache_hit: 0, projects: 2, cost: 0.0013 },
    rows,
    by_project: [project('holyword', 2, 1000), project('meditate', 1, 300)],
  },
}

describe('Costs breakdown', () => {
  test('projects list their agents underneath, with the repo owner', () => {
    const [holyword, meditate] = projectItems(data)
    expect(holyword.name).toBe('holyword')
    expect(holyword.sub).toBe('2 agents · codex · boundcorp')
    expect(holyword.children?.map(child => child.name)).toEqual(['Fix verses', 'Search'])
    expect(meditate.children?.map(child => child.name)).toEqual(['Timer'])
  })

  test('agents name their project; call types nest chat replies by bot', () => {
    expect(itemsFor('Agents', data)[0].sub).toBe('holyword · codex · gpt-6-sol · completed')
    const [chat] = itemsFor('Call types', data)
    expect(chat.children?.map(child => child.name)).toEqual(['devbox'])
  })

  test('distinguishes unknown and partial agent estimates from billed spend', () => {
    const headline = { ...data.agents.headline, cost: 0, priced_rows: 0, unpriced_rows: 1, billed_usd: null }
    const html = renderToStaticMarkup(
      createElement(Summary, { data: { ...data, agents: { ...data.agents, headline } } }),
    )
    expect(html).toContain('Unknown')
    expect(html).toContain('0 priced rows · 1 unpriced · billed USD unknown')
    const partial = { ...headline, cost: 1, priced_rows: 1 }
    const partialHtml = renderToStaticMarkup(
      createElement(Summary, { data: { ...data, agents: { ...data.agents, headline: partial } } }),
    )
    expect(partialHtml).toContain('$1.00')
    expect(partialHtml).toContain('1 priced rows · 1 unpriced · billed USD unknown')
  })

  test('renders the summary and the chosen tab', () => {
    const html = renderToStaticMarkup(
      createElement(MemoryRouter, null, createElement(CostsBody, { data, view: 'Projects', onView: () => {} })),
    )
    expect(html).toContain('Agent list price')
    expect(html).toContain('2 projects')
    expect(html).toContain('holyword')
    expect(html).toContain('aria-selected="true"')
  })
})

const scope = (over: Partial<EstimateScope> = {}): EstimateScope => ({
  rows: 2,
  input_tokens: 100,
  cache_write_tokens: 100,
  cache_read_tokens: 800,
  output_tokens: 5000,
  tokens: 6000,
  cache_denominator: 1000,
  cache_hit: 0.8,
  list_cost: 3,
  input_cost: 1,
  cache_write_cost: 0.5,
  cache_read_cost: 0.5,
  output_cost: 1,
  priced_rows: 2,
  unpriced_rows: 0,
  priced_tokens: 6000,
  unpriced_tokens: 0,
  unpriced_models: [],
  no_usage_rows: 0,
  exact_window: true,
  spillover_rows: 0,
  spillover_tokens: 0,
  first_at: null,
  last_at: null,
  ...over,
})

const withEstimate = (bot: EstimateScope, agents: EstimateScope, combined: EstimateScope): Costs => ({
  ...data,
  since: '2026-11-07T00:00:00+00:00',
  until: '2026-11-13T12:00:00+00:00',
  estimate: { basis: 'x', bot, agents, combined },
})
const render = (d: Costs) => renderToStaticMarkup(createElement(Estimate, { data: d }))

describe('List-price estimate', () => {
  test('shows uncached input, the cache denominator with output excluded, and each part cost', () => {
    const html = render(withEstimate(scope(), scope(), scope()))
    expect(html).toContain('100 uncached in')
    expect(html).toContain('80% cache hit (800 reads / 1k = uncached input + writes + reads; output excluded)')
    expect(html).toContain('out $1.00')
    expect(html).toContain('not an invoice or subscription quota')
    expect(html).toContain('2026-11-07 to 2026-11-13 UTC')
  })

  test('unpriced rows are unknown or a lower bound, never silently zero', () => {
    const unknown = scope({
      priced_rows: 0,
      unpriced_rows: 2,
      list_cost: 0,
      unpriced_tokens: 6000,
      unpriced_models: ['gpt-6.1-sol'],
    })
    const partial = scope({ unpriced_rows: 1, unpriced_tokens: 10, unpriced_models: ['mystery'] })
    const html = render(withEstimate(unknown, partial, partial))
    expect(html).toContain('Unknown')
    expect(html).toContain('6k tokens not costed: gpt-6.1-sol')
    expect(html).toContain('$3.00+')
  })

  test('missing usage and a zero denominator are reported, not shown as 0%', () => {
    const empty = scope({
      cache_hit: null,
      cache_denominator: 0,
      input_tokens: 0,
      cache_write_tokens: 0,
      cache_read_tokens: 0,
      no_usage_rows: 2,
    })
    const html = render(withEstimate(empty, empty, empty))
    expect(html).toContain('no cache denominator recorded')
    expect(html).toContain('2 rows recorded no token usage')
    expect(html).not.toContain('0% cache hit')
  })

  test('agent lifetime spillover is labelled, bot window is exact', () => {
    const lifetime = scope({
      exact_window: false,
      spillover_rows: 1,
      spillover_tokens: 4000,
      first_at: '2026-10-01T00:00:00+00:00',
      last_at: '2026-11-13T00:00:00+00:00',
    })
    const html = render(withEstimate(scope(), lifetime, lifetime))
    expect(html).toContain('Lifetime totals of rows active in the window, 2026-10-01 to 2026-11-13')
    expect(html).toContain('1 began earlier (4k tokens)')
    expect(html.match(/Lifetime totals/g)?.length).toBe(2)
  })
})
