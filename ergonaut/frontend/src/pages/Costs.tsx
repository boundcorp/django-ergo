import { Fragment, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import type { CostBucket, Costs, UsageThread } from '../api'
import { api } from '../api'

const RANGES = [7, 30, 90]

function money(value: number) {
  if (value === 0) return '$0'
  if (value < 0.0001) return '<$0.0001'
  // Cheap models cost fractions of a cent per call; keep two significant digits.
  if (value < 0.01) return `$${value.toPrecision(2)}`
  return value.toLocaleString(undefined, { style: 'currency', currency: 'USD', maximumFractionDigits: 2 })
}

function tokens(value: number) {
  return value >= 1_000_000
    ? `${(value / 1_000_000).toFixed(1)}M`
    : value >= 1000
      ? `${Math.round(value / 1000)}k`
      : String(value)
}

function Tile({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="surface-card min-h-28 px-5 py-4">
      <div className="text-xs font-semibold uppercase tracking-wide text-muted">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums">{value}</div>
      {note && <div className="mt-0.5 text-xs text-zinc-500">{note}</div>}
    </div>
  )
}

// Daily spend alternates the two chart hues in the handoff and keeps exact values on hover.
function Daily({ days }: { days: Costs['by_day'] }) {
  const [hover, setHover] = useState<number | null>(null)
  const max = Math.max(...days.map(d => d.cost), 0.0001)
  const shown = hover != null ? days[hover] : null
  return (
    <div>
      <div className={`mb-1 h-5 text-xs ${shown ? 'text-muted' : 'text-sm font-semibold text-ink'}`}>
        {shown ? `${shown.date}: ${money(shown.cost)} · ${shown.calls} calls` : 'Daily estimated spend'}
      </div>
      <div
        className="flex h-32 items-end gap-[2px] border-b border-zinc-200 dark:border-zinc-800"
        onMouseLeave={() => setHover(null)}
      >
        {days.map((d, i) => (
          <div key={d.date} className="flex h-full flex-1 items-end" onMouseEnter={() => setHover(i)}>
            <div
              className={`${i % 2 ? 'bg-mint' : 'bg-accent'} w-full rounded-t`}
              style={{ height: d.cost ? `${Math.max(2, (d.cost / max) * 100)}%` : 0 }}
            />
          </div>
        ))}
      </div>
      <div className="mt-1 flex justify-between text-[11px] text-zinc-500">
        <span>{days[0]?.date}</span>
        <span>{days[days.length - 1]?.date}</span>
      </div>
    </div>
  )
}

const PARTS = [
  ['input', 'Input'],
  ['cache_write', 'Cache write'],
  ['cache_read', 'Cache read'],
  ['output', 'Output'],
] as const

const PART_COLORS = ['bg-accent', 'bg-mint', 'bg-amber-500', 'bg-sky-500'] as const

function percent(value: number) {
  return `${Math.round(value * 100)}%`
}

function Usage({ data }: { data: Costs }) {
  const { headline, threads } = data.usage
  const [group, setGroup] = useState<'Thread' | 'Bot' | 'Model'>('Thread')
  const [sort, setSort] = useState<'share' | 'tokens' | 'cache_hit' | 'cost'>('share')
  const sorted = [...threads].sort((a, b) => b[sort] - a[sort] || a.title.localeCompare(b.title))
  const groups: { name: string; rows: UsageThread[] }[] = []
  for (const row of sorted) {
    const name = group === 'Bot' ? row.bot : group === 'Model' ? row.model : ''
    let bucket = groups.find(item => item.name === name)
    if (!bucket) {
      bucket = { name, rows: [] }
      groups.push(bucket)
    }
    bucket.rows.push(row)
  }
  const sortButton = (label: string, key: typeof sort) => (
    <button onClick={() => setSort(key)} className="hover:text-ink" aria-label={`Sort by ${label}`}>
      {label}
      {sort === key ? ' ↓' : ''}
    </button>
  )

  return (
    <section className="mt-8">
      <h2 className="font-display text-2xl font-semibold">Usage</h2>
      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-7">
        <Tile label="Threads" value={headline.threads.toLocaleString()} />
        <Tile label="Tokens" value={tokens(headline.tokens)} />
        <Tile label="Cache hit" value={percent(headline.cache_hit)} />
        <Tile label="Main chats" value={percent(headline.main_chats)} />
        <Tile label="Subscription" value={percent(headline.subscription)} note="Share of tokens" />
        <Tile label="API spend" value={money(headline.api_spend)} />
        <Tile label="Compaction" value={percent(headline.compaction)} note="Share of tokens" />
      </div>
      <div className="surface-card mt-4 p-5">
        <h3 className="text-sm font-semibold">Token mix</h3>
        <div className="mt-3 flex h-5 overflow-hidden rounded-control bg-raised" role="img" aria-label="Token mix">
          {PARTS.map(([part], i) => {
            const count = data.total[`${part}_tokens`]
            return (
              count > 0 && (
                <div key={part} className={PART_COLORS[i]} style={{ width: `${(100 * count) / headline.tokens}%` }} />
              )
            )
          })}
        </div>
        <div className="mt-3 flex flex-wrap gap-x-6 gap-y-2 text-xs text-muted">
          {PARTS.map(([part, label], i) => (
            <div key={part} className="flex items-center gap-2">
              <span className={`h-2.5 w-2.5 rounded-sm ${PART_COLORS[i]}`} />
              <span>
                {label}: {data.total[`${part}_tokens`].toLocaleString()}
              </span>
            </div>
          ))}
        </div>
      </div>
      <div className="mt-8 flex flex-wrap items-center justify-between gap-3">
        <h3 className="text-xl font-semibold">By thread</h3>
        <div className="flex gap-1 rounded-control border border-stroke bg-surface p-1" aria-label="Group threads by">
          {(['Thread', 'Bot', 'Model'] as const).map(option => (
            <button
              key={option}
              onClick={() => setGroup(option)}
              aria-pressed={group === option}
              className={`rounded-control px-3 py-1 text-sm ${group === option ? 'bg-accent font-semibold text-canvas' : 'text-muted hover:bg-raised'}`}
            >
              {option}
            </button>
          ))}
        </div>
      </div>
      <div className="mt-3 overflow-x-auto rounded-card border border-stroke">
        <table className="w-full min-w-[850px] text-sm">
          <thead className="border-b border-stroke text-xs text-muted">
            <tr>
              <th className="px-3 py-2 text-left font-normal">Thread</th>
              <th className="px-3 py-2 text-left font-normal">Bot</th>
              <th className="px-3 py-2 text-left font-normal">Model</th>
              <th className="px-3 py-2 text-left font-normal">Share bar</th>
              <th className="px-3 py-2 text-right font-normal">{sortButton('Tokens', 'tokens')}</th>
              <th className="px-3 py-2 text-right font-normal">{sortButton('Cache hit', 'cache_hit')}</th>
              <th className="px-3 py-2 text-right font-normal">{sortButton('Share', 'share')}</th>
              <th className="px-3 py-2 text-right font-normal">{sortButton('Cost', 'cost')}</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-stroke">
            {groups.map(bucket => (
              <Fragment key={bucket.name}>
                {bucket.name && (
                  <tr className="bg-raised">
                    <th colSpan={8} className="px-3 py-2 text-left font-semibold">
                      {bucket.name}
                    </th>
                  </tr>
                )}
                {bucket.rows.map(row => (
                  <tr key={row.id}>
                    <td className="max-w-56 truncate px-3 py-2">
                      <Link to={`/s/${row.id}`} className="text-accent hover:underline">
                        {row.title}
                      </Link>
                    </td>
                    <td className="px-3 py-2 text-muted">{row.bot}</td>
                    <td className="px-3 py-2 text-muted">{row.model}</td>
                    <td className="w-28 px-3 py-2">
                      <div className="h-2 rounded-full bg-raised">
                        <div className="h-full rounded-full bg-accent" style={{ width: `${100 * row.share}%` }} />
                      </div>
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums">{row.tokens.toLocaleString()}</td>
                    <td className="px-3 py-2 text-right tabular-nums">{percent(row.cache_hit)}</td>
                    <td className="px-3 py-2 text-right tabular-nums">{percent(row.share)}</td>
                    <td className="px-3 py-2 text-right tabular-nums">{row.subscription ? 'sub' : money(row.cost)}</td>
                  </tr>
                ))}
              </Fragment>
            ))}
            {!threads.length && (
              <tr>
                <td colSpan={8} className="px-3 py-4 text-muted">
                  No session calls in this period.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  )
}

export function AgentSessions({ data }: { data: Costs }) {
  const { headline, rows } = data.agents
  const totals = PARTS.map(([part]) => ({
    part,
    tokens: rows.reduce((sum, row) => sum + row[`${part}_tokens`], 0),
  }))
  return (
    <section className="mt-8">
      <h2 className="font-display text-2xl font-semibold">Agent sessions</h2>
      <div className="mt-4 grid gap-3 sm:grid-cols-3">
        <Tile label="Agent sessions" value={headline.sessions.toLocaleString()} />
        <Tile label="Tokens" value={tokens(headline.tokens)} />
        <Tile label="Cache hit" value={percent(headline.cache_hit)} />
      </div>
      <div className="surface-card mt-4 p-5">
        <h3 className="text-sm font-semibold">Token mix</h3>
        <div
          className="mt-3 flex h-5 overflow-hidden rounded-control bg-raised"
          role="img"
          aria-label="Agent token mix"
        >
          {totals.map(
            ({ part, tokens: count }, index) =>
              count > 0 && (
                <div
                  key={part}
                  className={PART_COLORS[index]}
                  style={{ width: `${(100 * count) / Math.max(1, headline.tokens)}%` }}
                />
              ),
          )}
        </div>
        <div className="mt-3 flex flex-wrap gap-x-6 gap-y-2 text-xs text-muted">
          {totals.map(({ part, tokens: count }, index) => (
            <div key={part} className="flex items-center gap-2">
              <span className={`h-2.5 w-2.5 rounded-sm ${PART_COLORS[index]}`} />
              <span>
                {PARTS[index][1]}: {count.toLocaleString()}
              </span>
            </div>
          ))}
        </div>
      </div>
      <div className="mt-4 overflow-x-auto rounded-card border border-stroke">
        <table className="w-full min-w-[850px] text-sm">
          <thead className="border-b border-stroke text-xs text-muted">
            <tr>
              <th className="px-3 py-2 text-left font-normal">Worker</th>
              <th className="px-3 py-2 text-left font-normal">Agent</th>
              <th className="px-3 py-2 text-left font-normal">Model</th>
              <th className="px-3 py-2 text-right font-normal">Tokens</th>
              <th className="px-3 py-2 text-right font-normal">Cache hit</th>
              <th className="px-3 py-2 text-right font-normal">Requests</th>
              <th className="px-3 py-2 text-left font-normal">Status</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-stroke">
            {rows.map(row => (
              <tr key={`${row.worker_id}-${row.model}`}>
                <td className="max-w-56 truncate px-3 py-2">
                  <Link to={`/s/${row.chat_id}`} className="text-accent hover:underline" title={row.chat_title}>
                    {row.worker_title}
                  </Link>
                </td>
                <td className="px-3 py-2 text-muted">{row.agent}</td>
                <td className="px-3 py-2 text-muted">{row.model}</td>
                <td className="px-3 py-2 text-right tabular-nums">{row.tokens.toLocaleString()}</td>
                <td className="px-3 py-2 text-right tabular-nums">{percent(row.cache_hit)}</td>
                <td className="px-3 py-2 text-right tabular-nums">{row.requests.toLocaleString()}</td>
                <td className="px-3 py-2 text-muted">{row.worker_status}</td>
              </tr>
            ))}
            {!rows.length && (
              <tr>
                <td colSpan={7} className="px-3 py-4 text-muted">
                  No agent sessions in this period.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  )
}

// Tokens, with what they cost at that part's rate on hover.
function PartCell({ bucket, part }: { bucket: CostBucket; part: (typeof PARTS)[number][0] }) {
  const count = bucket[`${part}_tokens`]
  const reasoning =
    part === 'output' && bucket.reasoning_tokens ? ` · ${tokens(bucket.reasoning_tokens)} of it reasoning` : ''
  return (
    <td
      className="py-1.5 text-right tabular-nums"
      title={count ? `${money(bucket[`${part}_cost`])}${reasoning}` : undefined}
    >
      {count ? tokens(count) : <span className="text-zinc-400">–</span>}
    </td>
  )
}

function Row({
  bucket,
  nested,
  toggle,
  open,
}: {
  bucket: CostBucket
  nested?: boolean
  toggle?: () => void
  open?: boolean
}) {
  return (
    <tr className={nested ? 'text-zinc-600 dark:text-zinc-400' : ''}>
      <td className={`py-1.5 font-mono text-xs ${nested ? 'pl-6' : ''}`}>
        {toggle ? (
          <button onClick={toggle} className="hover:underline">
            {open ? '▾' : '▸'} {bucket.name}
          </button>
        ) : (
          bucket.name
        )}
      </td>
      <td className="py-1.5 text-right tabular-nums">{bucket.calls}</td>
      {PARTS.map(([part]) => (
        <PartCell key={part} bucket={bucket} part={part} />
      ))}
      <td className="py-1.5 text-right font-medium tabular-nums">
        {money(bucket.cost)}
        {bucket.unpriced_calls > 0 && (
          <span className="ml-1 text-amber-600" title={`${bucket.unpriced_calls} calls with no price`}>
            *
          </span>
        )}
      </td>
    </tr>
  )
}

function Table({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mt-8 data-scroll">
      <h2 className="mb-3 text-xl font-semibold">{title}</h2>
      <div className="max-w-4xl overflow-x-auto rounded-card border border-stroke">
        <table className="w-full text-sm">
          <thead className="text-xs text-zinc-500">
            <tr className="border-b border-zinc-200 dark:border-zinc-800">
              <th className="py-1 text-left font-normal">Name</th>
              <th className="py-1 text-right font-normal">Calls</th>
              {PARTS.map(([part, label]) => (
                <th key={part} className="py-1 text-right font-normal">
                  {label}
                </th>
              ))}
              <th className="py-1 text-right font-normal">Cost</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-100 dark:divide-zinc-900">{children}</tbody>
        </table>
      </div>
    </section>
  )
}

export function CostsPage() {
  const [days, setDays] = useState(7)
  const [data, setData] = useState<Costs | null>(null)
  const [error, setError] = useState('')
  const [open, setOpen] = useState(true)
  const [bot, setBot] = useState('')
  const [bots, setBots] = useState<string[]>([])

  useEffect(() => {
    api
      .bots()
      .then(items => setBots(items.map(item => item.name)))
      .catch(() => {})
  }, [])

  useEffect(() => {
    let active = true
    setData(null)
    setError('')
    api
      .costs(days, bot)
      .then(result => {
        if (active) setData(result)
      })
      .catch(e => {
        if (active) setError(String(e.message ?? e))
      })
    return () => {
      active = false
    }
  }, [days, bot])

  if (!data) return <div className="p-6 text-zinc-500">{error || 'Loading…'}</div>

  const { total } = data
  return (
    <div className="page-content h-full overflow-y-auto">
      <div className="flex flex-wrap items-start gap-4">
        <div>
          <p className="eyebrow">Usage</p>
          <h1 className="page-title mt-3">Costs &amp; usage</h1>
          <p className="page-lede mt-3">Estimated usage across bots, models, and call types.</p>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <select
            aria-label="Filter by bot"
            value={bot}
            onChange={e => setBot(e.target.value)}
            className="rounded-control border border-stroke bg-surface px-3 py-2 text-sm"
          >
            <option value="">All bots</option>
            {bots.map(name => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
          <div className="flex gap-1 rounded-card border border-stroke bg-surface p-1">
            {RANGES.map(r => (
              <button
                key={r}
                onClick={() => setDays(r)}
                aria-pressed={r === days}
                className={`rounded-control px-4 py-2 text-sm ${r === days ? 'bg-accent font-semibold text-canvas' : 'text-muted hover:bg-raised'}`}
              >
                {r} days
              </button>
            ))}
          </div>
        </div>
      </div>

      <Usage data={data} />
      <AgentSessions data={data} />

      <h2 className="mt-12 font-display text-2xl font-semibold">Costs</h2>
      <div className="mt-6 grid max-w-4xl grid-cols-1 gap-4 sm:grid-cols-3">
        <Tile label="Estimated spend" value={money(total.cost)} note={`Last ${data.days} days`} />
        <Tile label="Calls" value={total.calls.toLocaleString()} />
        <Tile
          label="Prompt served from cache"
          value={`${Math.round((100 * total.cache_read_tokens) / Math.max(1, total.input_tokens + total.cache_write_tokens + total.cache_read_tokens))}%`}
          note={`${tokens(total.cache_read_tokens)} of ${tokens(total.input_tokens + total.cache_write_tokens + total.cache_read_tokens)} input tokens`}
        />
      </div>

      <div className="mt-4 grid max-w-4xl grid-cols-2 gap-4 sm:grid-cols-4">
        {PARTS.map(([part, label]) => (
          <Tile
            key={part}
            label={label}
            value={money(total[`${part}_cost`])}
            note={`${tokens(total[`${part}_tokens`])} tokens`}
          />
        ))}
      </div>

      <div className="surface-card mt-8 max-w-4xl p-5">
        <Daily days={data.by_day} />
      </div>

      <Table title="By kind">
        {data.by_kind.map(bucket => (
          <Fragment key={bucket.name}>
            {bucket.name === 'chat_reply' ? (
              <>
                <Row bucket={bucket} toggle={() => setOpen(!open)} open={open} />
                {open && data.chat_reply_by_bot.map(bot => <Row key={bot.name} bucket={bot} nested />)}
              </>
            ) : (
              <Row bucket={bucket} />
            )}
          </Fragment>
        ))}
        {!data.by_kind.length && (
          <tr>
            <td colSpan={7} className="py-3 text-zinc-500">
              No calls in this period.
            </td>
          </tr>
        )}
      </Table>

      <Table title="By model">
        {data.by_model.map(bucket => (
          <Row key={bucket.name} bucket={bucket} />
        ))}
      </Table>

      {!!data.unpriced_models.length && (
        <p className="mt-4 max-w-3xl text-xs text-amber-700 dark:text-amber-400">
          * No price for {data.unpriced_models.join(', ')}; those calls count as $0. Add prices under
          DJANGO_ERGO["MODEL_PRICES"].
        </p>
      )}
      <p className="mt-2 max-w-3xl text-xs text-zinc-500">
        Costs are worked out from token counts and list prices, so they are estimates. Input, cache writes, cache reads
        and output are each billed at the model's own rate; hover a token count to see its cost. OpenAI calls made
        before cached tokens were tracked count them as full-price input.
      </p>
    </div>
  )
}
