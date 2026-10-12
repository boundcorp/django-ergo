import { describe, expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import type { AgentUsage, CostBucket, Costs } from '../api'
import { CostsBody, itemsFor, projectItems } from './Costs'

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
