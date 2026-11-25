import { useCallback, useEffect, useState } from 'react'
import type { ChatPools, PoolRefusal, PoolRow, PoolSettings, PoolStatePatch } from '../api'
import { api } from '../api'

// Model pools (django_ergo.bots.pools, docs/model-pools.md): the Routing page's Pools section,
// the chat's pool picker and the refusal a chat stops on. Every list here comes from the same
// eligibility rules the backend selects with.

const STATUS_LABEL: Record<string, [string, string]> = {
  eligible: ['Eligible', 'bg-teal-tint text-teal'],
  pool_disabled: ['Switched off', 'bg-raised text-muted'],
  budget_required: ['No budget', 'bg-amber-tint text-warning'],
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

function Budget({ pool, editable, onSave }: { pool: PoolRow; editable: boolean; onSave: (p: PoolStatePatch) => void }) {
  const [budget, setBudget] = useState(pool.budget_usd ?? '')
  const [expires, setExpires] = useState(pool.expires_at ? pool.expires_at.slice(0, 16) : '')
  const [perLaunch, setPerLaunch] = useState(pool.per_launch_usd ?? '')
  useEffect(() => {
    setBudget(pool.budget_usd ?? '')
    setExpires(pool.expires_at ? pool.expires_at.slice(0, 16) : '')
    setPerLaunch(pool.per_launch_usd ?? '')
  }, [pool.budget_usd, pool.expires_at, pool.per_launch_usd])
  if (!pool.metered) return <p className="text-xs text-muted">Subscriptions only: no budget needed.</p>
  return (
    <div className="flex flex-col gap-2 text-sm">
      <p className="text-xs text-muted">
        Spent ${Number(pool.spent_usd ?? 0).toFixed(2)} · reserved ${Number(pool.reserved_usd ?? 0).toFixed(2)}
        {pool.budget_usd != null ? ` of $${pool.budget_usd}` : ' · no budget: metered routes refuse to send'}
      </p>
      {editable && (
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={e => {
            e.preventDefault()
            const patch: PoolStatePatch = { version: pool.version }
            if (budget === '') patch.clear_budget = true
            else patch.budget_usd = String(budget)
            if (expires === '') patch.clear_expiry = true
            else patch.expires_at = new Date(expires).toISOString()
            if (perLaunch !== '') patch.per_launch_usd = String(perLaunch)
            onSave(patch)
          }}
        >
          <label className="flex flex-col text-xs">
            Budget (USD)
            <input
              inputMode="decimal"
              className="w-24 rounded-control border border-stroke bg-transparent px-2 py-1"
              value={budget}
              onChange={e => setBudget(e.target.value)}
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
            Expires
            <input
              type="datetime-local"
              className="rounded-control border border-stroke bg-transparent px-2 py-1"
              value={expires}
              onChange={e => setExpires(e.target.value)}
            />
          </label>
          <button type="submit" className="min-h-9 rounded-control border border-stroke px-3 text-xs hover:bg-raised">
            Save budget
          </button>
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
  const editable = !!data.editable
  const order = data.pools.map(p => p.name)
  function move(index: number, by: number) {
    const next = [...order]
    const [item] = next.splice(index, 1)
    next.splice(index + by, 0, item)
    save(api.updatePoolPreferences({ version: data!.user_version, order: next }))
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
              ? `Using ${data.pools.find(p => p.name === data.selected)?.label ?? data.selected} (picked by hand)`
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
            onChange={value => save(api.updatePoolPreferences({ version: data.user_version, auto_rollover: value }))}
          />
          <p className="text-xs text-muted">
            Rollover needs three gates: this switch, the exhausted pool's “rolls over”, and the next pool's “automatic
            entry”. Errors other than exhaustion (switched off, no key, privacy, budget, expiry) never roll over.
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <span>Active pool:</span>
            <select
              className="rounded-control border border-stroke bg-transparent px-2 py-1"
              value={data.selected ?? ''}
              aria-label="Active pool"
              onChange={e =>
                save(api.updatePoolPreferences({ version: data.user_version, active_pool: e.target.value }))
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
                  disabled={!editable}
                  onChange={value => save(api.updatePool(pool.name, { version: pool.version, enabled: value }))}
                />
                <Toggle
                  label="Rolls over when exhausted"
                  checked={!!pool.rollover_out}
                  disabled={!editable}
                  onChange={value => save(api.updatePool(pool.name, { version: pool.version, rollover_out: value }))}
                />
                <Toggle
                  label="Automatic entry"
                  checked={!!pool.allow_auto_entry}
                  disabled={!editable}
                  hint="Rollover may move work into this pool without asking"
                  onChange={value =>
                    save(api.updatePool(pool.name, { version: pool.version, allow_auto_entry: value }))
                  }
                />
              </div>
            )}
            <Budget
              pool={pool}
              editable={editable && !data.legacy}
              onSave={patch => save(api.updatePool(pool.name, patch))}
            />
            <p className="text-xs text-muted">
              {pool.models.map(m => (
                <span key={m.id} className={`mr-2 font-mono ${m.available ? '' : 'line-through'}`} title={m.why}>
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
                  disabled={!editable || !p.configured_enabled}
                  hint={p.configured_enabled ? '' : 'Switched off in providers.yaml'}
                  onChange={value => save(api.updateProvider(p.name, value))}
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
      const result = await api.setChatPool(sessionId, pool, scope, data?.user_version)
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
            <option key={p.name} value={p.name} disabled={!p.enabled}>
              {p.label}
              {p.status !== 'eligible' ? ` — ${STATUS_LABEL[p.status]?.[0] ?? p.status}` : ''}
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
          <button
            type="button"
            className="underline"
            onClick={async () => {
              if (data.chat_pool) await pick('', 'chat')
              if (data.selected) await pick('', 'user')
            }}
          >
            Use preferred order
          </button>
        )}
      </div>
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
                  await api.setChatPool(sessionId, c.name, 'chat')
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
