import { afterEach, beforeEach, expect, test } from 'bun:test'
import { act, createElement } from 'react'
import type { Root } from 'react-dom/client'
import { renderToStaticMarkup } from 'react-dom/server'
import { Window } from 'happy-dom'
import { api, type MonthlyProviderBudget, type PoolFunding } from '../api'
import { MonthlyProviderForm } from './MonthlyFunding'

const details: PoolFunding = {
  pool: 'paid',
  owner: 'lee',
  envelope_id: 'envelope',
  pending: true,
  configured_ceiling_usd: '300',
  configured_expires_at: '2099-12-31T00:00:00Z',
  accounts: [{ identity: 'synthetic-account', label: 'OpenAI account', provider_type: 'openai' }],
  spent_usd: '0',
  reserved_usd: '0',
  approval: null,
}
const snapshot: MonthlyProviderBudget = {
  family: 'openai',
  owner_id: 'owner',
  actor_id: 'actor',
  timezone: 'UTC',
  month: '2026-11-01',
  version: 4,
  monthly_usd: '100',
  enabled: true,
  spent_usd: '80',
  reserved_usd: '15',
  valid_from: '2026-01-01T00:00:00Z',
  expires_at: '2099-01-01T00:00:00Z',
  history_problem: '',
  accounts: [
    {
      id: 1,
      identity: 'synthetic-account',
      account_label: 'Work account',
      actor_id: 'admin',
      assertion: 'Explicit assertion',
    },
  ],
}
let win: Window
let root: Root
let container: HTMLDivElement
const oldRead = api.monthlyProviderBudget
const oldWrite = api.setMonthlyProviderConsent
beforeEach(async () => {
  win = new Window({ url: 'http://localhost' })
  Object.assign(globalThis, {
    window: win,
    document: win.document,
    navigator: win.navigator,
    HTMLElement: win.HTMLElement,
    Event: win.Event,
    MouseEvent: win.MouseEvent,
    IS_REACT_ACT_ENVIRONMENT: true,
  })
  container = win.document.createElement('div')
  win.document.body.append(container)
  root = (await import('react-dom/client')).createRoot(container)
})
afterEach(() => {
  act(() => root.unmount())
  api.monthlyProviderBudget = oldRead
  api.setMonthlyProviderConsent = oldWrite
  win.happyDOM.close()
})

test('admin assertions stay blank and imply no recurring consent', () => {
  const html = renderToStaticMarkup(createElement(MonthlyProviderForm, { family: 'openai', details, admin: true }))
  expect(html).toContain('all bots, threads, models, accounts and keys')
  expect(html).toContain('Maximum $100 each UTC calendar month')
  expect(html).toContain('Never enter or compare API key values')
  expect(html).toContain('disabled="">Assert immutable account mapping')
  expect(html).not.toContain('Save recurring consent')
  expect(html).not.toContain('checked=""')
})

test('inspection displays holds, actor and month without approving anything', async () => {
  let writes = 0
  api.monthlyProviderBudget = async () => snapshot
  api.setMonthlyProviderConsent = async () => {
    writes++
    return snapshot
  }
  await act(async () => root.render(createElement(MonthlyProviderForm, { family: 'openai', details, admin: false })))
  expect(container.textContent).toContain('$15 held')
  expect(container.textContent).toContain('month 2026-11-01')
  expect(container.textContent).toContain('consenting actor actor')
  expect(container.textContent).toContain('asserted by admin')
  expect(container.querySelector<HTMLInputElement>('[aria-label="openai monthly USD"]')!.value).toBe('')
  expect(container.querySelector<HTMLButtonElement>('button')!.disabled).toBe(true)
  expect(container.querySelector('select')).toBeNull()
  expect(writes).toBe(0)
})

test('a stale recurring write stays visible and uses the inspected version', async () => {
  api.monthlyProviderBudget = async () => snapshot
  let version = 0
  api.setMonthlyProviderConsent = async (_family, body) => {
    version = body.version
    expect(body.timezone).toBe('UTC')
    expect(body.enabled).toBe(false)
    throw new Error('Recurring consent changed; refresh before writing')
  }
  await act(async () => root.render(createElement(MonthlyProviderForm, { family: 'openai', details, admin: false })))
  await act(async () =>
    container.querySelector('form')!.dispatchEvent(new win.Event('submit', { bubbles: true, cancelable: true })),
  )
  expect(version).toBe(4)
  expect(container.querySelector('[role="alert"]')?.textContent).toContain('refresh before writing')
  expect(container.textContent).toContain('Version 4')
})
