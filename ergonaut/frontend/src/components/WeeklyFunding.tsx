import { useEffect, useState } from 'react'
import type { WeeklyProviderBudget, PoolFunding } from '../api'
import { api } from '../api'
import { fromLocalInput } from '../localTime'

const families: Record<string, string> = { openai: 'openai', google: 'google', claude: 'anthropic' }
const hosts: Record<string, string> = {
  'api.openai.com': 'openai',
  'generativelanguage.googleapis.com': 'google',
  'api.anthropic.com': 'anthropic',
}

/** The vendor an account is billed by: its endpoint host when first-party, else its wire type. */
export function vendorOf(account: { identity: string; provider_type?: string }): string {
  try {
    const endpoint = String(JSON.parse(account.identity)[4] ?? '')
    const host = new URL(endpoint).hostname.toLowerCase()
    if (hosts[host]) return hosts[host]
  } catch {
    // fall back to the wire type
  }
  return families[account.provider_type || ''] ?? ''
}

export function WeeklyFunding({ details, admin }: { details: PoolFunding; admin: boolean }) {
  const names = [...new Set(details.accounts.map(vendorOf).filter(Boolean))]
  return (
    <div>
      {names.map(family => (
        <WeeklyProviderForm key={family} family={family} details={details} admin={admin} />
      ))}
    </div>
  )
}

export function WeeklyProviderForm({
  family,
  details,
  admin,
}: {
  family: string
  details: PoolFunding
  admin: boolean
}) {
  const [data, setData] = useState<WeeklyProviderBudget | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [identity, setIdentity] = useState('')
  const [label, setLabel] = useState('')
  const [assertion, setAssertion] = useState('')
  const [attempts, setAttempts] = useState('')
  const [amount, setAmount] = useState('')
  const [expiry, setExpiry] = useState('')
  const [enabled, setEnabled] = useState(false)
  const [note, setNote] = useState('')
  const [acknowledged, setAcknowledged] = useState(false)
  useEffect(() => {
    let cancelled = false
    api
      .weeklyProviderBudget(family)
      .then(value => {
        if (!cancelled) setData(value)
      })
      .catch(e => {
        if (!cancelled) setError(String(e.message || e))
      })
    return () => {
      cancelled = true
    }
  }, [family])
  async function write(fn: () => Promise<WeeklyProviderBudget>) {
    setBusy(true)
    setError('')
    try {
      setData(await fn())
      setAcknowledged(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <section className="border border-stroke rounded-control p-3 my-2 flex flex-col gap-2 text-xs">
      <p className="font-semibold">{family}: recurring provider budget · UTC</p>
      <p>
        One aggregate cap across all bots, threads, models, accounts and keys. Maximum $100 each UTC ISO week (Monday
        00:00 to the next Monday 00:00 UTC). Unknown holds from prior weeks reduce availability until reconciled.
      </p>
      {data && (
        <p>
          Version {data.version} · {data.enabled ? 'Enabled' : 'Disabled'} · ${data.weekly_usd ?? '0'} weekly · $
          {data.spent_usd} spent · ${data.reserved_usd} held · week of {data.week}. Owner {data.owner_id}; consenting
          actor {data.actor_id ?? 'none'}.
        </p>
      )}
      {data?.accounts.map(a => (
        <p key={a.id}>
          Account {a.account_label} · asserted by {a.actor_id} · {a.assertion}
        </p>
      ))}
      {data?.history_problem && <p role="alert">{data.history_problem}</p>}
      {admin && (
        <form
          className="flex flex-col gap-1"
          onSubmit={e => {
            e.preventDefault()
            void write(() =>
              api.assertWeeklyMapping({
                identity,
                owner: details.owner,
                account_label: label,
                assertion,
                attempt_ids: attempts.split(/[\s,]+/).filter(Boolean),
                family,
              }),
            )
          }}
        >
          <p className="font-semibold">Admin account assertion</p>
          <p>
            Assert the billing vendor and account using metadata only. Never enter or compare API key values. Owner:{' '}
            {details.owner}.
          </p>
          <select
            aria-label={`${family} account identity`}
            required
            value={identity}
            onChange={e => setIdentity(e.target.value)}
          >
            <option value="">Choose account and model</option>
            {details.accounts
              .filter(a => vendorOf(a) === family)
              .map(a => (
                <option key={a.identity} value={a.identity}>
                  {a.label}
                </option>
              ))}
          </select>
          <input
            aria-label={`${family} account label`}
            placeholder="Billing account label"
            maxLength={200}
            required
            value={label}
            onChange={e => setLabel(e.target.value)}
          />
          <input
            aria-label={`${family} mapping assertion`}
            placeholder="Explicit vendor/account assertion"
            maxLength={200}
            required
            value={assertion}
            onChange={e => setAssertion(e.target.value)}
          />
          <textarea
            aria-label={`${family} historical attempt IDs`}
            placeholder="Historical paid attempt UUIDs, if explicitly reconciled to this account"
            value={attempts}
            onChange={e => setAttempts(e.target.value)}
          />
          <button disabled={busy || !identity || !label || !assertion}>Assert immutable account mapping</button>
        </form>
      )}
      {data && (
        <form
          className="flex flex-col gap-1"
          onSubmit={e => {
            e.preventDefault()
            void write(() =>
              api.setWeeklyProviderConsent(family, {
                version: data.version,
                weekly_usd: amount,
                enabled,
                valid_from: new Date().toISOString(),
                expires_at: fromLocalInput(expiry),
                timezone: 'UTC',
                note,
              }),
            )
          }}
        >
          <label>
            Recurring USD per UTC ISO week{' '}
            <input
              aria-label={`${family} weekly USD`}
              type="number"
              min="0"
              max="100"
              step="0.0001"
              required
              value={amount}
              onChange={e => setAmount(e.target.value)}
            />
          </label>
          <label>
            Consent expiry{' '}
            <input
              aria-label={`${family} consent expiry`}
              type="datetime-local"
              required
              value={expiry}
              onChange={e => setExpiry(e.target.value)}
            />
          </label>
          <label>
            <input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />
            Enable this provider independently
          </label>
          <input
            aria-label={`${family} consent note`}
            placeholder="Consent note"
            required
            maxLength={200}
            value={note}
            onChange={e => setNote(e.target.value)}
          />
          <label>
            <input type="checkbox" checked={acknowledged} onChange={e => setAcknowledged(e.target.checked)} />I
            explicitly authorize these recurring weekly terms until expiry, subject to all existing privacy, pause and
            grant restrictions.
          </label>
          <button disabled={busy || !acknowledged || !amount || !expiry || !note}>
            Save recurring consent v{data.version + 1}
          </button>
        </form>
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  )
}
