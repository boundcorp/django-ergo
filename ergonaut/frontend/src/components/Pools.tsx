import { useCallback, useEffect, useState } from 'react'
import type {
  ChatPools,
  FundingApproval,
  PoolEnvelopeCandidates,
  PoolFunding,
  PoolGrant,
  PoolRefusal,
  PoolRow,
  PoolSettings,
} from '../api'
import { api } from '../api'
import { fromLocalInput, toLocalInput } from '../localTime'
import { MonthlyFunding } from './MonthlyFunding'

// Model pools (django_ergo.bots.pools, docs/model-pools.md): the Routing page's Pools section,
// the chat's pool picker and the refusal a chat stops on. Every list here comes from the same
// eligibility rules the backend selects with.

const STATUS_LABEL: Record<string, [string, string]> = {
  eligible: ['Eligible', 'bg-teal-tint text-teal'],
  pool_disabled: ['Switched off', 'bg-raised text-muted'],
  budget_required: ['No grant', 'bg-amber-tint text-warning'],
  not_authorized: ['Not yours', 'bg-raised text-muted'],
  grant_not_yet_valid: ['Grant not started', 'bg-amber-tint text-warning'],
  no_capacity: ['No capacity now', 'bg-red-tint text-danger'],
  unguarded: ['No spend guard', 'bg-red-tint text-danger'],
  budget_exhausted: ['Over budget', 'bg-red-tint text-danger'],
  budget_expired: ['Expired', 'bg-red-tint text-danger'],
  pool_exhausted: ['Exhausted', 'bg-red-tint text-danger'],
}

function StatusBadge({ status }: { status: string }) {
  const [label, tone] = STATUS_LABEL[status] ?? [status.replace(/_/g, ' '), 'bg-raised text-muted']
  return <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${tone}`}>{label}</span>
}

function message(e: unknown) {
  return e instanceof Error ? e.message : String(e)
}

function Toggle({
  label,
  checked,
  disabled,
  onChange,
  hint,
}: {
  label: string
  checked: boolean
  disabled?: boolean
  onChange: (value: boolean) => void
  hint?: string
}) {
  return (
    <label className="flex items-center gap-2 text-sm" title={hint}>
      <input
        type="checkbox"
        className="accent-indigo-600"
        checked={checked}
        disabled={disabled}
        onChange={e => onChange(e.target.checked)}
      />
      <span>{label}</span>
    </label>
  )
}

function money(value: string | null | undefined) {
  return `$${Number(value ?? 0).toFixed(2)}`
}

function GrantLine({ grant, onRevoke }: { grant: PoolGrant; onRevoke?: () => void }) {
  return (
    <li className="flex flex-wrap items-center gap-2 text-xs">
      <span>
        {money(grant.spent_usd)} spent · {money(grant.reserved_usd)} reserved of ${grant.budget_usd}
        {grant.per_launch_usd != null ? ` · ${money(grant.per_launch_usd)} per coding launch` : ''}
      </span>
      <span className="text-muted">
        {new Date(grant.valid_from).toLocaleString()} to {new Date(grant.expires_at).toLocaleString()}
        {grant.source === 'config' ? ' · explicit funding, limited by configuration' : ''}
      </span>
      <span className="font-mono text-muted">{grant.routes.join(', ')}</span>
      {onRevoke && (
        <button type="button" className="underline" onClick={onRevoke}>
          Revoke
        </button>
      )}
    </li>
  )
}

/** A pool's spend authority: your own grant, and for its owner the grants issued and a form to issue one. */
function Grants({
  pool,
  onIssue,
  onRevoke,
}: {
  pool: PoolRow
  onIssue: (body: { grantee: string; budget_usd: string; expires_at: string; per_launch_usd?: string }) => void
  onRevoke: (id: string) => void
}) {
  const [grantee, setGrantee] = useState('')
  const [budget, setBudget] = useState('')
  const [perLaunch, setPerLaunch] = useState('')
  // datetime-local is local wall-clock time; the server gets UTC (localTime.ts).
  const [expires, setExpires] = useState(toLocalInput(pool.grant?.expires_at))
  useEffect(() => setExpires(toLocalInput(pool.grant?.expires_at)), [pool.grant?.expires_at])
  if (!pool.metered) return <p className="text-xs text-muted">Subscriptions only: no spend grant needed.</p>
  return (
    <div className="flex flex-col gap-2 text-sm">
      {pool.grant ? (
        <ul>
          <GrantLine grant={pool.grant} onRevoke={pool.manageable ? undefined : () => onRevoke(pool.grant!.id)} />
        </ul>
      ) : (
        <p className="text-xs text-muted">You have no spend grant here: its metered routes refuse to send.</p>
      )}
      {!!pool.grants?.length && (
        <div>
          <p className="text-xs font-semibold">Grants you issued</p>
          <ul className="flex flex-col gap-1">
            {pool.grants.map(g => (
              <GrantLine key={g.id} grant={g} onRevoke={() => onRevoke(g.id)} />
            ))}
          </ul>
        </div>
      )}
      {pool.manageable && (
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={e => {
            e.preventDefault()
            const body: { grantee: string; budget_usd: string; expires_at: string; per_launch_usd?: string } = {
              grantee: grantee.trim(),
              budget_usd: budget.trim(),
              expires_at: fromLocalInput(expires),
            }
            if (perLaunch.trim()) body.per_launch_usd = perLaunch.trim()
            onIssue(body)
          }}
        >
          <label className="flex flex-col text-xs">
            Grantee (username)
            <input
              className="w-32 rounded-control border border-stroke bg-transparent px-2 py-1"
              value={grantee}
              onChange={e => setGrantee(e.target.value)}
              required
            />
          </label>
          <label className="flex flex-col text-xs">
            Budget (USD)
            <input
              inputMode="decimal"
              className="w-24 rounded-control border border-stroke bg-transparent px-2 py-1"
              value={budget}
              onChange={e => setBudget(e.target.value)}
              required
            />
          </label>
          <label className="flex flex-col text-xs">
            Per coding launch (USD)
            <input
              inputMode="decimal"
              className="w-24 rounded-control border border-stroke bg-transparent px-2 py-1"
              value={perLaunch}
              onChange={e => setPerLaunch(e.target.value)}
            />
          </label>
          <label className="flex flex-col text-xs">
            Ends (your local time)
            <input
              type="datetime-local"
              className="rounded-control border border-stroke bg-transparent px-2 py-1"
              value={expires}
              onChange={e => setExpires(e.target.value)}
              required
            />
          </label>
          <button type="submit" className="min-h-9 rounded-control border border-stroke px-3 text-xs hover:bg-raised">
            Issue grant
          </button>
          <p className="w-full text-xs text-muted">
            A new grant replaces the grantee's current one on this pool and starts a new ledger.
          </p>
        </form>
      )}
    </div>
  )
}

function resets(data: PoolSettings, pool: PoolRow): string {
  const times: number[] = []
  for (const route of pool.routes ?? []) {
    const provider = route.split('/')[0]
    for (const at of Object.values(data.reset_times?.[provider] ?? {})) times.push(at)
  }
  const next = times.filter(t => t * 1000 > Date.now()).sort()[0]
  return next ? `quota resets ${new Date(next * 1000).toLocaleString()}` : ''
}

/** Consent fields intentionally start empty; config ceilings are information only. */
export function FundingForm({
  details,
  busy,
  onApprove,
}: {
  details: PoolFunding
  busy: boolean
  onApprove: (body: FundingApproval) => Promise<void>
}) {
  const [amount, setAmount] = useState('')
  const [period, setPeriod] = useState<'cumulative' | 'calendar_month_utc'>('cumulative')
  const [expiry, setExpiry] = useState('')
  const [accounts, setAccounts] = useState<string[]>([])
  const [note, setNote] = useState('')
  const [error, setError] = useState('')
  return (
    <form
      className="flex flex-col gap-2 text-sm"
      onSubmit={async e => {
        e.preventDefault()
        try {
          setError('')
          await onApprove({
            budget_usd: amount.trim(),
            expires_at: fromLocalInput(expiry),
            accounts,
            note,
            budget_period: period,
            version: details.version ?? 0,
          })
          setAmount('')
          setExpiry('')
          setAccounts([])
          setNote('')
        } catch (e) {
          setError(message(e))
        }
      }}
    >
      <p className="font-semibold">{details.pending ? 'Pending funding' : 'Funding approval'}</p>
      <p className="text-xs text-muted">
        Configured ceiling: {details.configured_ceiling_usd == null ? 'missing' : money(details.configured_ceiling_usd)}
        . Family use: {money(details.spent_usd)} spent and {money(details.reserved_usd)} reserved, including unknown
        outcomes.
      </p>
      {details.approval && (
        <p className="text-xs">
          Approved {details.approval.budget_period === 'calendar_month_utc' ? 'recurring UTC monthly' : 'cumulative'}{' '}
          cap {money(details.approval.budget_usd)} until {new Date(details.approval.expires_at).toLocaleString()}.
        </p>
      )}
      <p className="text-xs text-muted">
        Approve only the amount, expiry and accounts you authorize. Changing this approval keeps all prior use and
        holds.
      </p>
      <label className="text-xs">
        Funding period
        <select aria-label="Funding period" value={period} onChange={e => setPeriod(e.target.value as typeof period)}>
          <option value="cumulative">Cumulative lifetime cap</option>
          <option value="calendar_month_utc">Recurring calendar month (UTC)</option>
        </select>
      </label>
      {period === 'calendar_month_utc' && (
        <p className="text-xs">
          Explicit recurring approval renews each UTC calendar month until expiry. Each provider also requires admin
          account mapping and independent recurring consent; prior unresolved holds carry forward.
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <label className="flex flex-col text-xs">
          {period === 'cumulative' ? 'Approve cumulative USD cap' : 'Approve recurring USD monthly cap'}
          <input
            aria-label="Approve cumulative USD cap"
            className="rounded-control border border-stroke bg-transparent px-2 py-1"
            type="number"
            min="0.0001"
            max="99999999.9999"
            step="0.0001"
            required
            value={amount}
            onChange={e => setAmount(e.target.value)}
          />
        </label>
        <label className="flex flex-col text-xs">
          Funding expiry
          <input
            aria-label="Funding expiry"
            className="rounded-control border border-stroke bg-transparent px-2 py-1"
            type="datetime-local"
            required
            value={expiry}
            onChange={e => setExpiry(e.target.value)}
          />
        </label>
      </div>
      <fieldset disabled={busy} className="flex flex-col gap-1">
        <legend className="text-xs font-semibold">Accounts and models to approve</legend>
        {details.accounts.map(account => (
          <label key={account.identity} className="text-xs flex items-center gap-2">
            <input
              type="checkbox"
              checked={accounts.includes(account.identity)}
              onChange={e =>
                setAccounts(previous =>
                  e.target.checked ? [...previous, account.identity] : previous.filter(a => a !== account.identity),
                )
              }
            />
            {account.label}
          </label>
        ))}
      </fieldset>
      <label className="text-xs">
        Approval note{' '}
        <input
          aria-label="Approval note"
          maxLength={200}
          className="rounded-control border border-stroke bg-transparent px-2 py-1"
          value={note}
          onChange={e => setNote(e.target.value)}
        />
      </label>
      <button
        type="submit"
        className="self-start rounded-control border border-stroke px-3 py-1"
        disabled={busy || !accounts.length || !amount.trim() || !expiry}
      >
        Approve funding
      </button>
      {error && (
        <p role="alert" className="text-xs text-danger">
          {error}
        </p>
      )}
    </form>
  )
}

function FundingControls({
  name,
  admin,
  onChanged,
}: {
  name: string
  admin: boolean
  onChanged: (data: PoolSettings) => void
}) {
  const [details, setDetails] = useState<PoolFunding | null>(null)
  const [candidates, setCandidates] = useState<PoolEnvelopeCandidates | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')
  const load = useCallback(async () => {
    try {
      setDetails(await api.poolFunding(name))
      if (admin) setCandidates(await api.poolEnvelopes(name))
      setError('')
    } catch (e) {
      setError(message(e))
    }
  }, [name, admin])
  useEffect(() => {
    void load()
  }, [load])
  async function mutate(change: () => Promise<PoolSettings>) {
    setBusy(true)
    try {
      onChanged(await change())
      await load()
      setNote('')
    } catch (e) {
      setError(message(e))
      throw e
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="rounded-control border border-stroke p-3 flex flex-col gap-3">
      {details && (
        <FundingForm details={details} busy={busy} onApprove={body => mutate(() => api.approveFunding(name, body))} />
      )}
      {details && <MonthlyFunding details={details} admin={admin} />}
      {admin && candidates && (
        <div className="flex flex-col gap-2 text-xs">
          <p className="font-semibold">Admin: reconcile funding history</p>
          {candidates.problem && <p>{candidates.problem}</p>}
          <p>
            Continue preserves an existing family's cap and holds. Prepare separate family creates no spend approval.
            Restore lifts a revocation and adds no funding.
          </p>
          <label>
            Admin note{' '}
            <input
              aria-label="Admin funding note"
              maxLength={200}
              value={note}
              onChange={e => setNote(e.target.value)}
              className="rounded-control border border-stroke bg-transparent px-2 py-1"
            />
          </label>
          {candidates.envelopes.map(envelope => (
            <div key={envelope.id} className="flex flex-wrap items-center gap-2">
              <span>
                {envelope.pool}: {money(envelope.spent_usd)} spent, {money(envelope.reserved_usd)} held
                {envelope.in_use ? ' · used by a current pool' : ''}
              </span>
              <button
                type="button"
                disabled={busy || !envelope.continuable || !!details?.envelope_id}
                className="underline"
                onClick={() => {
                  void mutate(() => api.linkPoolEnvelope(name, { envelope: envelope.id, note })).catch(() => {})
                }}
              >
                Continue existing family
              </button>
            </div>
          ))}
          <div className="flex flex-wrap gap-3">
            <button
              type="button"
              disabled={busy}
              className="underline"
              onClick={() => {
                void mutate(() => api.linkPoolEnvelope(name, { fund: true, note })).catch(() => {})
              }}
            >
              {details?.envelope_id ? 'Approve account linkage (no funding)' : 'Prepare separate family'}
            </button>
            <button
              type="button"
              disabled={busy}
              className="underline"
              onClick={() => {
                void mutate(() => api.linkPoolEnvelope(name, { restore: true, note })).catch(() => {})
              }}
            >
              Restore revoked authority
            </button>
          </div>
        </div>
      )}
      {error && (
        <p role="alert" className="text-xs text-danger">
          {error}
        </p>
      )}
    </div>
  )
}

export function changePaidPause(
  data: PoolSettings,
  confirmResume: (message: string) => boolean,
  send = api.setPaidPause,
): Promise<PoolSettings> | null {
  if (data.paid_pause_version === undefined) throw new Error('Pause state is missing; refresh the page.')
  if (data.paid_paused && !confirmResume('Resume paid dispatch? Existing funding and holds remain in effect.'))
    return null
  return send(!data.paid_paused, data.paid_pause_version)
}

/** The Routing page's Pools section: order, toggles, budgets, the rollover gate, provider switches. */
export function PoolsSection() {
  const [data, setData] = useState<PoolSettings | null>(null)
  const [error, setError] = useState('')
  const [adminPool, setAdminPool] = useState('')
  const [adminTarget, setAdminTarget] = useState('')
  const load = useCallback(() => {
    api
      .pools()
      .then(d => {
        setData(d)
        setError('')
      })
      .catch(e => setError(message(e)))
  }, [])
  useEffect(() => {
    load()
  }, [load])
  async function save(change: Promise<PoolSettings>) {
    try {
      setData(await change)
      setError('')
    } catch (e) {
      setError(message(e))
      api
        .pools()
        .then(setData)
        .catch(() => {})
    }
  }
  if (!data) return error ? <p className="text-sm text-danger">{error}</p> : null
  const order = data.pools.map(p => p.name)
  function move(index: number, by: number) {
    const next = [...order]
    const [item] = next.splice(index, 1)
    next.splice(index + by, 0, item)
    save(api.updatePoolPreferences({ version: data!.user_version ?? 0, order: next }))
  }
  return (
    <section aria-labelledby="routing-pools">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="routing-pools" className="font-display text-2xl font-semibold">
          Pools
        </h2>
        <span className="text-sm text-muted">
          {data.legacy
            ? 'providers.yaml has no pools section: every provider is one pool, as before.'
            : data.selected
              ? `Using ${data.pools.find(p => p.name === data.selected)?.label ?? data.selected} (${
                  data.rolled_from
                    ? `rolled over from ${data.pools.find(p => p.name === data.rolled_from)?.label ?? data.rolled_from}`
                    : 'picked by hand'
                })`
              : `Automatic: ${data.pools.find(p => p.name === data.active)?.label ?? 'none usable'}`}
        </span>
      </div>
      {error && (
        <p role="alert" className="mt-2 text-sm text-danger">
          {error}
        </p>
      )}
      {(data.paid_paused || data.enforcement_activated) && (
        <p className="mt-2 text-sm">
          {data.paid_paused
            ? 'Paid dispatch paused. Grants and unknown holds are retained.'
            : 'Pool enforcement is active. Removing configuration does not restore legacy paid dispatch.'}
        </p>
      )}
      {data.admin && (
        <div className="surface-card mt-4 flex flex-col gap-2 p-4 text-sm">
          <button
            type="button"
            className="self-start rounded-control border border-stroke px-3 py-1"
            disabled={data.paid_pause_version === undefined}
            onClick={() => {
              const change = changePaidPause(data, message => window.confirm(message))
              if (change) save(change)
            }}
          >
            {data.paid_paused ? 'Resume paid dispatch' : 'Pause paid dispatch'}
          </button>
          <form
            className="flex gap-2"
            onSubmit={e => {
              e.preventDefault()
              setAdminTarget(adminPool.trim())
            }}
          >
            <label className="text-xs">
              Manage funding for pool{' '}
              <input
                aria-label="Admin pool name"
                value={adminPool}
                onChange={e => setAdminPool(e.target.value)}
                className="rounded-control border border-stroke bg-transparent px-2 py-1"
                required
              />
            </label>
            <button type="submit" className="underline">
              Open funding controls
            </button>
          </form>
          {adminTarget && <FundingControls key={adminTarget} name={adminTarget} admin onChanged={setData} />}
        </div>
      )}
      {!data.legacy && (
        <div className="surface-card mt-4 flex flex-col gap-2 p-4 text-sm">
          <Toggle
            label="Automatic rollover to the next pool when one is exhausted"
            checked={!!data.auto_rollover}
            onChange={value =>
              save(api.updatePoolPreferences({ version: data.user_version ?? 0, auto_rollover: value }))
            }
          />
          <p className="text-xs text-muted">
            Rollover needs three gates: this switch, the exhausted pool's “rolls over”, and the next pool's “automatic
            entry”. Errors other than exhaustion (switched off, no key, privacy, grant, expiry) never roll over. After a
            rollover work stays on the new pool until you switch back or pick “Use preferred order”.
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <span>Active pool:</span>
            <select
              className="rounded-control border border-stroke bg-transparent px-2 py-1"
              value={data.selected ?? ''}
              aria-label="Active pool"
              onChange={e =>
                save(api.updatePoolPreferences({ version: data.user_version ?? 0, active_pool: e.target.value }))
              }
            >
              <option value="">Use preferred order</option>
              {data.pools.map(p => (
                <option key={p.name} value={p.name}>
                  {p.label}
                </option>
              ))}
            </select>
          </div>
        </div>
      )}
      <ol className="mt-4 flex flex-col gap-3">
        {data.pools.map((pool, index) => (
          <li key={pool.name} className="surface-card flex flex-col gap-3 p-4">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-xs text-muted">{index + 1}</span>
              <span className="font-semibold">{pool.label}</span>
              <StatusBadge status={pool.status} />
              {pool.name === data.active && <span className="text-xs text-accent-soft">active</span>}
              {pool.metered && <span className="text-xs text-warning">metered</span>}
              {pool.owner && <span className="text-xs text-muted">{pool.is_owner ? 'yours' : `${pool.owner}'s`}</span>}
              {!pool.selectable_now && pool.status === 'eligible' && (
                <span className="text-xs text-muted">nothing selectable now</span>
              )}
              <span className="text-xs text-muted">{resets(data, pool)}</span>
              {!data.legacy && (
                <span className="ml-auto flex gap-1">
                  <button
                    type="button"
                    aria-label={`Move ${pool.label} up`}
                    disabled={index === 0}
                    onClick={() => move(index, -1)}
                    className="rounded-control border border-stroke px-2 disabled:opacity-40"
                  >
                    ↑
                  </button>
                  <button
                    type="button"
                    aria-label={`Move ${pool.label} down`}
                    disabled={index === data.pools.length - 1}
                    onClick={() => move(index, 1)}
                    className="rounded-control border border-stroke px-2 disabled:opacity-40"
                  >
                    ↓
                  </button>
                </span>
              )}
            </div>
            {pool.why && <p className="text-xs text-muted">{pool.why}</p>}
            {!data.legacy && (
              <div className="flex flex-wrap gap-4">
                <Toggle
                  label="On"
                  checked={!!pool.enabled}
                  disabled={!pool.manageable}
                  onChange={value => save(api.updatePool(pool.name, { version: pool.version ?? 0, enabled: value }))}
                />
                <Toggle
                  label="Rolls over when exhausted"
                  checked={!!pool.rollover_out}
                  disabled={!pool.manageable}
                  onChange={value =>
                    save(api.updatePool(pool.name, { version: pool.version ?? 0, rollover_out: value }))
                  }
                />
                <Toggle
                  label="Automatic entry"
                  checked={!!pool.allow_auto_entry}
                  disabled={!pool.manageable}
                  hint="Rollover may move work into this pool without asking"
                  onChange={value =>
                    save(api.updatePool(pool.name, { version: pool.version ?? 0, allow_auto_entry: value }))
                  }
                />
              </div>
            )}
            {pool.funding_pending && (
              <p className="text-xs text-warning">Pending funding: configured budgets do not authorize spending.</p>
            )}
            {!data.legacy && pool.metered && pool.manageable && (
              <FundingControls name={pool.name} admin={!!data.admin} onChanged={setData} />
            )}
            {!data.legacy && (
              <Grants
                pool={pool}
                onIssue={body => save(api.issueGrant(pool.name, body))}
                onRevoke={id => save(api.revokeGrant(id))}
              />
            )}
            <p className="text-xs text-muted">
              {pool.models.map(m => (
                <span
                  key={m.id}
                  className={`mr-2 font-mono ${m.available ? '' : 'line-through'}`}
                  title={m.available ? 'selectable now' : `configured, not selectable now: ${m.why}`}
                >
                  {m.id}
                </span>
              ))}
            </p>
          </li>
        ))}
      </ol>
      {!!data.providers?.length && (
        <div className="surface-card mt-4 p-4">
          <h3 className="text-sm font-semibold">Providers</h3>
          <ul className="mt-2 flex flex-wrap gap-4">
            {data.providers.map(p => (
              <li key={p.name} className="flex items-center gap-2 text-sm">
                <Toggle
                  label={`${p.name} (${p.billing}${p.available ? '' : ', unavailable'})`}
                  checked={p.enabled}
                  disabled={!data.admin || !p.configured_enabled}
                  hint={p.configured_enabled ? '' : 'Switched off in providers.yaml'}
                  onChange={value => save(api.updateProvider(p.name, value, p.version ?? 0))}
                />
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}

/** The chat's pool: "Auto: Subscriptions", a pick for this chat or as the default, or a privacy lock. */
export function PoolPicker({ sessionId, onChanged }: { sessionId: string; onChanged: () => void }) {
  const [data, setData] = useState<ChatPools | null>(null)
  const [note, setNote] = useState('')
  const load = useCallback(() => {
    api
      .chatPools(sessionId)
      .then(setData)
      .catch(() => setData(null))
  }, [sessionId])
  useEffect(() => {
    load()
  }, [load])
  if (!data || data.legacy) return null
  if (data.privacy?.restricted) {
    return (
      <div className="flex items-center gap-2 rounded-card border border-stroke bg-surface px-4 py-3 text-sm">
        <span aria-hidden="true">🔒</span>
        <span>
          Privacy: {data.privacy.label}. This chat stays on{' '}
          {data.pools.map(p => p.label).join(', ') || 'its private models'}; switching pools doesn't apply.
        </span>
      </div>
    )
  }
  const active = data.pools.find(p => p.name === data.active)
  async function pick(pool: string, scope: 'chat' | 'user') {
    try {
      const result = await api.setChatPool(sessionId, pool, scope, data?.chat_version ?? 0, data?.user_version ?? 0)
      setNote(result.warning)
    } catch (e) {
      setNote(message(e))
    }
    load()
    onChanged()
  }
  return (
    <div className="flex flex-col gap-2 rounded-card border border-stroke bg-surface px-4 py-3 text-sm">
      <label className="flex items-center gap-2 font-semibold">
        <span aria-hidden="true">Pool ·</span>
        <select
          aria-label="Pool for this chat"
          className="min-w-0 max-w-full rounded-control border-0 bg-transparent py-0.5 font-semibold"
          value={data.chat_pool}
          onChange={e => pick(e.target.value, 'chat')}
        >
          <option value="">
            {data.selected
              ? `Default: ${data.pools.find(p => p.name === data.selected)?.label}`
              : `Auto: ${active?.label ?? 'none'}`}
          </option>
          {data.pools.map(p => (
            <option key={p.name} value={p.name} disabled={!p.enabled || p.status === 'not_authorized'}>
              {p.label}
              {p.status !== 'eligible'
                ? ` — ${STATUS_LABEL[p.status]?.[0] ?? p.status}`
                : p.selectable_now === false
                  ? ' — nothing selectable now'
                  : ''}
              {p.metered ? ' (metered)' : ''}
            </option>
          ))}
        </select>
      </label>
      <div className="flex flex-wrap gap-2 text-xs">
        {data.chat_pool && (
          <button type="button" className="underline" onClick={() => pick(data.chat_pool, 'user')}>
            Make {data.pools.find(p => p.name === data.chat_pool)?.label} my default
          </button>
        )}
        {(data.selected || data.chat_pool) && (
          <button type="button" className="underline" onClick={() => pick('', data.selected ? 'user' : 'chat')}>
            Use preferred order
          </button>
        )}
      </div>
      {data.rolled_from && data.selected && (
        <p className="text-xs text-warning">
          Rolled over from {data.pools.find(p => p.name === data.rolled_from)?.label ?? data.rolled_from}; work stays on{' '}
          {data.pools.find(p => p.name === data.selected)?.label ?? data.selected} until you switch back or use
          preferred order.
        </p>
      )}
      {data.route?.route && (
        <p className="text-xs text-muted">
          Last turn: {data.route.route} in {data.route.pool_label || data.route.pool}
          {data.route.rollover?.length ? ` (rolled over from ${data.route.rollover.join(', ')})` : ''}
        </p>
      )}
      {note && <p className="text-xs text-warning">{note}</p>}
    </div>
  )
}

/** A chat stopped on a pool refusal: why, and a button per pool it could switch to. */
export function PoolRefusalBanner({
  sessionId,
  refusal,
  busy,
  onSwitched,
}: {
  sessionId: string
  refusal: PoolRefusal
  busy?: boolean
  onSwitched: () => void
}) {
  const [error, setError] = useState('')
  const choices = refusal.next_choices.filter(c => c.status === 'eligible' || c.status === 'pool_exhausted')
  return (
    <div
      role="alert"
      className="mx-4 mb-2 rounded-card border border-warning/40 bg-amber-tint px-4 py-3 text-sm sm:mx-6"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">
          {refusal.code === 'pool_exhausted' ? 'Pool exhausted. ' : 'Model pool refused this turn. '}
          {refusal.message}
        </span>
        <span className="ml-auto flex flex-wrap gap-2">
          {choices.map(c => (
            <button
              key={c.name}
              type="button"
              disabled={busy}
              className="rounded-control border border-accent px-3 py-1 text-xs font-semibold text-accent disabled:opacity-50"
              title={c.metered ? 'A metered pool: its budget applies' : ''}
              onClick={async () => {
                try {
                  const current = await api.chatPools(sessionId)
                  await api.setChatPool(sessionId, c.name, 'chat', current.chat_version)
                  setError('')
                  onSwitched()
                } catch (e) {
                  setError(message(e))
                }
              }}
            >
              Switch to {c.label}
              {c.metered ? ' (metered)' : ''}
            </button>
          ))}
        </span>
      </div>
      {error && <p className="mt-1 text-xs text-danger">{error}</p>}
    </div>
  )
}
