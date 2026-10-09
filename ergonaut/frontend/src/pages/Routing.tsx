import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import type { Routing, RoutingCandidate, RoutingProvider, RoutingWindow } from '../api'
import { api } from '../api'
import { ago, agoLong } from '../time'
import { CapacityLoading, CapacitySection } from './Capacity'

function windowLabel(key: string, window?: RoutingWindow) {
  if (window?.label) return window.label
  const labels: Record<string, string> = {
    five_hour: '5-hour',
    weekly: 'Weekly',
    weekly_fable: 'Weekly · Fable',
  }
  return labels[key] ?? key.replace(/[_-]+/g, ' ').replace(/^./, c => c.toUpperCase())
}

const STATUS: Record<RoutingProvider['status'], [string, string]> = {
  in_use: ['In use', 'bg-teal-tint text-teal'],
  standby: ['Standby', 'bg-indigo-tint text-accent-soft'],
  skipped: ['Skipped', 'bg-red-tint text-danger'],
  api_key: ['Pay per token', 'bg-amber-tint text-warning'],
  unavailable: ['Unavailable', 'bg-raised text-muted'],
}

// Subscription accounts and their limits are the capacity section (Capacity.tsx); this lists the
// providers billed per token, which the router doesn't meter.
export function Providers({ providers }: { providers: RoutingProvider[] }) {
  const apiKeys = providers.filter(p => !p.subscription)
  if (!apiKeys.length) return null
  return (
    <section aria-labelledby="routing-api-keys">
      <h2 id="routing-api-keys" className="font-display text-2xl font-semibold">
        Pay-per-token providers
      </h2>
      <div className="mt-4 grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr))]">
        {apiKeys.map(p => {
          const [label, tone] = STATUS[p.status]
          return (
            <article key={p.name} className="surface-card flex flex-col gap-3 p-5">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <h3 className="font-display text-lg font-semibold">{p.name}</h3>
                  <p className="text-sm text-muted">{p.api_key_env ? `API key · ${p.api_key_env}` : `${p.type} API`}</p>
                </div>
                <span className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-semibold ${tone}`}>{label}</span>
              </div>
              <p className="text-sm text-muted">
                Billed per token. Bot chats use this only when their tier lists it; coding agents never do.
              </p>
              <div className="mt-auto space-y-1 border-t border-stroke pt-3 text-xs text-muted">
                {p.reason && <p>{p.reason}</p>}
                <p>Not metered by the router.</p>
              </div>
            </article>
          )
        })}
      </div>
    </section>
  )
}

// A chat candidate names its provider; an agent's label already names the agent.
const name = (c: RoutingCandidate) => (c.ref ? `${c.provider} · ${c.label}` : c.label)

function Candidate({ c }: { c: RoutingCandidate }) {
  const base =
    'relative inline-flex items-center gap-1.5 rounded-control px-3 py-1.5 font-mono text-xs whitespace-nowrap'
  const tone = {
    pick: 'bg-accent font-semibold text-canvas',
    ok: 'border border-stroke bg-raised',
    skip: 'border border-dashed border-stroke text-muted line-through',
    unavailable: 'border border-dashed border-stroke text-muted',
  }[c.state]
  const title = c.reason || (c.state === 'pick' ? 'A new turn gets this now' : '')
  return (
    <li className={`${base} ${tone}`} title={title}>
      {c.state === 'pick' && <span aria-hidden="true">●</span>}
      {name(c)}
      {c.state === 'pick' && <span className="sr-only"> (picked now)</span>}
      {c.state === 'skip' && <span className="sr-only"> (skipped: {c.reason})</span>}
      {c.state === 'unavailable' && <span className="sr-only"> (unavailable: {c.reason})</span>}
    </li>
  )
}

function Candidates({ list }: { list: RoutingCandidate[] }) {
  if (!list.length) return <span className="text-sm text-muted">Not set</span>
  const notes = list.filter(c => c.reason && c.state !== 'ok')
  return (
    <>
      <ol className="flex flex-wrap items-center gap-2">
        {list.map((c, i) => (
          <Candidate key={i} c={c} />
        ))}
      </ol>
      {notes.length > 0 && (
        <p className="mt-2 text-xs text-muted">{notes.map(c => `${name(c)}: ${c.reason}`).join('. ')}</p>
      )}
    </>
  )
}

export function Tiers({ data }: { data: Routing }) {
  const names = [...new Set([...data.tiers.map(t => t.name), ...data.agents.map(a => a.name)])]
  if (!names.length)
    return (
      <p className="surface-card p-5 text-sm text-muted">
        providers.yaml has no <code>tiers</code> or <code>agents</code> yet. See “Routing by tier” in the building-bots
        guide.
      </p>
    )
  return (
    <div className="surface-card data-scroll">
      <table className="w-full text-left text-sm">
        <thead className="text-xs text-muted">
          <tr>
            <th scope="col" className="w-32 font-medium">
              Tier
            </th>
            <th scope="col" className="font-medium">
              Bot chats
            </th>
            <th scope="col" className="font-medium">
              Coding agents (Orca)
            </th>
          </tr>
        </thead>
        <tbody>
          {names.map(name => {
            const tier = data.tiers.find(t => t.name === name)
            const agents = data.agents.find(a => a.name === name)
            return (
              <tr key={name} className="border-t border-stroke align-top">
                <th scope="row" className="font-normal">
                  <div className="font-display text-base font-semibold">{name}</div>
                  {tier && (
                    <div className="text-xs text-muted">
                      {tier.chats} {tier.chats === 1 ? 'chat' : 'chats'}
                    </div>
                  )}
                </th>
                <td>
                  <Candidates list={tier?.candidates ?? []} />
                </td>
                <td>
                  <Candidates list={agents?.candidates ?? []} />
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export function Priorities({ data, onSaved }: { data: Routing; onSaved: (r: Routing) => void }) {
  const [draft, setDraft] = useState(data.text)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => setDraft(data.text), [data.text])
  const dirty = draft.trim() !== data.text.trim()

  const run = async (call: () => Promise<Routing>) => {
    setBusy(true)
    setError('')
    try {
      onSaved(await call())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const source =
    data.text_source === 'page'
      ? `Saved here${data.updated_at ? ` ${agoLong(data.updated_at)}` : ''}, in place of routing.md.`
      : data.text_source === 'file'
        ? 'From routing.md in the bot repo.'
        : 'No routing text yet.'
  const status = !data.text
    ? 'Using the limits in providers.yaml.'
    : data.compiling
      ? 'Compiling. The providers.yaml limits apply until it finishes.'
      : data.compile_error
        ? `Couldn't compile: ${data.compile_error}. The providers.yaml limits apply.`
        : data.compiled
          ? 'Compiled. These rules are in force.'
          : 'Not compiled yet. It compiles on the next routed turn.'
  const limits = data.rules.limits

  return (
    <div className="flex flex-wrap gap-4">
      <div className="surface-card flex min-w-0 flex-[3_1_420px] flex-col gap-3 p-5">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <label htmlFor="routing-text" className="font-semibold">
            Priorities in plain words
          </label>
          <span className="text-sm text-muted">{source}</span>
        </div>
        <textarea
          id="routing-text"
          rows={7}
          value={draft}
          onChange={e => setDraft(e.target.value)}
          readOnly={!data.editable}
          placeholder="Lean on Claude until its 5-hour window is 85% used. Keep Codex's weekly window under 80%."
          className="w-full resize-y rounded-control border border-stroke bg-canvas p-3.5 font-mono text-sm leading-relaxed"
        />
        {error && (
          <p role="alert" className="text-sm text-danger">
            {error}
          </p>
        )}
        {data.editable ? (
          <div className="flex flex-wrap justify-end gap-2">
            {data.text_source === 'page' && (
              <button
                type="button"
                disabled={busy}
                onClick={() => run(api.resetRouting)}
                className="min-h-11 rounded-control border border-stroke px-4 text-sm font-medium hover:bg-raised disabled:opacity-50"
              >
                Use routing.md
              </button>
            )}
            <button
              type="button"
              disabled={busy || !dirty}
              onClick={() => setDraft(data.text)}
              className="min-h-11 rounded-control border border-stroke px-4 text-sm font-medium hover:bg-raised disabled:opacity-50"
            >
              Discard
            </button>
            <button
              type="button"
              disabled={busy || !dirty}
              onClick={() => run(() => api.saveRouting(draft))}
              className="min-h-11 rounded-control bg-accent px-4 text-sm font-semibold text-canvas disabled:opacity-50"
            >
              {busy ? 'Saving…' : 'Save and compile'}
            </button>
          </div>
        ) : (
          <p className="text-sm text-muted">Only an admin can change routing.</p>
        )}
      </div>
      <div className="surface-card flex min-w-0 flex-[2_1_300px] flex-col gap-3 p-5">
        <div className="font-semibold">Rules in force</div>
        <p className="text-sm text-muted" aria-live="polite">
          {status}
        </p>
        <ul className="flex flex-col gap-2">
          {limits.map((l, i) => (
            <li key={i} className="flex justify-between gap-3 rounded-control bg-raised px-3.5 py-3 text-sm">
              <span>
                {l.provider === '*' ? 'Every provider' : l.provider} ·{' '}
                {windowLabel(l.window, data.providers.find(p => p.name === l.provider)?.windows[l.window])} window
              </span>
              <span className="font-mono text-warning">skip at {l.max_used}%</span>
            </li>
          ))}
          <li className="flex justify-between gap-3 rounded-control bg-raised px-3.5 py-3 text-sm">
            <span>{limits.length ? 'Everything else' : 'Every window'}</span>
            <span className="font-mono text-muted">skip at {data.default_max_used}%</span>
          </li>
        </ul>
        <p className="text-xs text-muted">
          Candidates are tried in the order providers.yaml lists them. Coding agents only ever use subscriptions.
        </p>
      </div>
    </div>
  )
}

export function RoutingPage() {
  const [data, setData] = useState<Routing | null>(null)
  const [error, setError] = useState('')
  const [loadedAt, setLoadedAt] = useState(Date.now())
  const [refreshing, setRefreshing] = useState(false)
  const [refreshError, setRefreshError] = useState('')
  const load = useCallback(
    () =>
      api
        .routing()
        .then(r => {
          setData(r)
          setLoadedAt(Date.now())
          setError('')
        })
        .catch(e => setError(e instanceof Error ? e.message : String(e))),
    [],
  )
  const refresh = useCallback(async () => {
    setRefreshing(true)
    setRefreshError('')
    try {
      setData(await api.refreshRouting())
      setLoadedAt(Date.now())
      setError('')
    } catch (e) {
      setRefreshError(e instanceof Error ? e.message : String(e))
    } finally {
      setRefreshing(false)
    }
  }, [])
  useEffect(() => {
    load()
  }, [load])
  // Follow a compile or a running sync, then keep usage current.
  const busy = !!data?.compiling || !!data?.capacity.sync.running
  useEffect(() => {
    const timer = setInterval(load, busy ? 2000 : 30000)
    return () => clearInterval(timer)
  }, [load, busy])

  return (
    <div className="page-content h-full overflow-y-auto">
      <p className="eyebrow">Settings</p>
      <h1 className="page-title mt-3">Routing &amp; capacity</h1>
      <p className="page-lede mt-3 max-w-2xl">
        Provider limits and reset windows used for model routing. Values reflect the latest successful source sync.
        Chats set to an Auto model try their tier's models in order, using subscriptions with room or configured
        pay-per-token providers; Orca workers started with a tier use subscriptions only.
      </p>
      {error && data && (
        <p role="alert" className="mt-6 text-sm text-danger">
          Couldn't reach Ergonaut: {error}. Showing what loaded{' '}
          <time dateTime={new Date(loadedAt).toISOString()}>{agoLong(new Date(loadedAt).toISOString())}</time>; limits
          below may be out of date.
        </p>
      )}
      {error && !data && (
        <div role="alert" className="surface-card mt-8 flex flex-wrap items-center justify-between gap-3 p-5">
          <p className="text-sm text-danger">
            <span aria-hidden="true">✕ </span>Couldn't load routing and limits: {error}
          </p>
          <button
            type="button"
            onClick={load}
            className="min-h-11 rounded-control border border-stroke px-4 text-sm font-medium hover:bg-raised"
          >
            Try again
          </button>
        </div>
      )}
      {!data ? (
        !error && (
          <div className="mt-8">
            <CapacityLoading />
          </div>
        )
      ) : (
        <div className="mt-8 flex flex-col gap-8">
          <CapacitySection
            capacity={data.capacity}
            refreshing={refreshing}
            refreshError={refreshError}
            onRefresh={refresh}
          />
          <Providers providers={data.providers} />

          <section aria-labelledby="routing-tiers">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <h2 id="routing-tiers" className="font-display text-2xl font-semibold">
                Tiers
              </h2>
              <span className="text-sm text-muted">
                Tried left to right. The highlighted model is what a new turn gets now.
              </span>
            </div>
            <div className="mt-4">
              <Tiers data={data} />
            </div>
          </section>

          <section aria-labelledby="routing-rules">
            <h2 id="routing-rules" className="font-display text-2xl font-semibold">
              Priorities
            </h2>
            <div className="mt-4">
              <Priorities data={data} onSaved={setData} />
            </div>
          </section>

          <section aria-labelledby="routing-switches">
            <h2 id="routing-switches" className="font-display text-2xl font-semibold">
              Recent switches
            </h2>
            {data.switches.length ? (
              <div className="surface-card data-scroll mt-4">
                <table className="w-full text-left text-sm">
                  <thead className="text-xs text-muted">
                    <tr>
                      <th scope="col" className="font-medium">
                        When
                      </th>
                      <th scope="col" className="font-medium">
                        Chat or worker
                      </th>
                      <th scope="col" className="font-medium">
                        Tier
                      </th>
                      <th scope="col" className="font-medium">
                        Moved to
                      </th>
                      <th scope="col" className="font-medium">
                        Why
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.switches.map((s, i) => (
                      <tr key={i} className="border-t border-stroke">
                        <td className="whitespace-nowrap font-mono text-muted" title={new Date(s.at).toLocaleString()}>
                          {ago(s.at)}
                        </td>
                        <td>
                          {s.session_id ? (
                            <Link to={`/s/${s.session_id}`} className="text-accent-soft hover:text-ink">
                              {s.label}
                            </Link>
                          ) : (
                            s.label
                          )}
                        </td>
                        <td>{s.tier}</td>
                        <td className="font-mono">
                          {s.to}
                          {s.from && <div className="text-xs text-muted">from {s.from}</div>}
                        </td>
                        <td className="text-muted">{s.reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="mt-4 text-sm text-muted">No chat or worker has been moved off its first choice yet.</p>
            )}
          </section>
        </div>
      )}
    </div>
  )
}
