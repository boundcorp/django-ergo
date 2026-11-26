import { useCallback, useEffect, useState } from 'react'
import type { ChatPools, PoolGrant, PoolRefusal, PoolRow, PoolSettings } from '../api'
import { api } from '../api'
import { fromLocalInput, toLocalInput } from '../localTime'

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
        {grant.source === 'config' ? ' · from providers.yaml' : ''}
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

/** The Routing page's Pools section: order, toggles, budgets, the rollover gate, provider switches. */
export function PoolsSection() {
  const [data, setData] = useState<PoolSettings | null>(null)
  const [error, setError] = useState('')
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
      load()
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
