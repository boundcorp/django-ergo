import { describe, expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import type { PoolFunding } from '../api'
import { FundingForm } from './Pools'

const details: PoolFunding = {
  pool: 'paid',
  owner: 'lee',
  envelope_id: 'family',
  pending: true,
  configured_ceiling_usd: '50',
  configured_expires_at: '2099-12-31T00:00:00Z',
  accounts: [{ identity: 'exact-account-model', label: 'openai/model · work-account' }],
  spent_usd: '1',
  reserved_usd: '2',
  approval: null,
}

describe('explicit funding consent form', () => {
  test('configured cap and expiry never prefill authorization or select an account', () => {
    const html = renderToStaticMarkup(createElement(FundingForm, { details, busy: false, onApprove: async () => {} }))
    expect(html).toContain('Pending funding')
    expect(html).toContain('Configured ceiling: $50.00')
    expect(html).toContain('including unknown outcomes')
    expect(html).toContain('openai/model · work-account')
    expect(html).not.toContain('checked=""')
    expect(html).not.toContain('value="50"')
    expect(html).not.toContain('value="2099')
    expect(html).toContain('disabled="">Approve funding')
  })
  test('an existing approval is a summary and leaves the new consent fields blank', () => {
    const funded = {
      ...details,
      pending: false,
      approval: {
        id: 1,
        budget_usd: '7',
        valid_from: '2026-01-01T00:00:00Z',
        expires_at: '2099-01-01T00:00:00Z',
        accounts: ['exact-account-model'],
        actor_id: 1,
      },
    }
    const html = renderToStaticMarkup(
      createElement(FundingForm, { details: funded, busy: false, onApprove: async () => {} }),
    )
    expect(html).toContain('Approved cumulative cap $7.00')
    expect(html).not.toContain('value="7"')
    expect(html).not.toContain('checked=""')
    expect(html).toContain('Changing this approval keeps all prior use and holds.')
  })
  test('a recurring approval keeps its kind in the form and conversion is explicit', () => {
    const weekly = {
      ...details,
      pending: false,
      version: 4,
      approval: {
        id: 4,
        budget_period: 'iso_week_utc' as const,
        budget_usd: '100',
        valid_from: '2026-01-01T00:00:00Z',
        expires_at: '2099-01-01T00:00:00Z',
        accounts: ['exact-account-model'],
        actor_id: 1,
      },
    }
    const html = renderToStaticMarkup(
      createElement(FundingForm, { details: weekly, busy: false, onApprove: async () => {} }),
    )
    expect(html).toContain('<option value="iso_week_utc" selected="">')
    expect(html).not.toContain('<option value="cumulative" selected="">')
    expect(html).not.toContain('Convert funding period') // same kind: no conversion
  })
})
