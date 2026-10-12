import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import type { AgentProject, AgentUsage, CostBucket, Costs, UsageThread } from '../api'
import { api } from '../api'

const RANGES = [7, 30, 90]

export function money(value: number) {
  if (value === 0) return '$0'
  if (value < 0.0001) return '<$0.0001'
  // Cheap models cost fractions of a cent per call; keep two significant digits.
  if (value < 0.01) return `$${value.toPrecision(2)}`
  return value.toLocaleString(undefined, { style: 'currency', currency: 'USD', maximumFractionDigits: 2 })
}

export function tokens(value: number) {
  return value >= 1_000_000
    ? `${(value / 1_000_000).toFixed(1)}M`
    : value >= 1000
      ? `${Math.round(value / 1000)}k`
      : String(value)
}

function percent(value: number) {
  return `${Math.round(value * 100)}%`
}

const PARTS = [
  ['input', 'Input'],
  ['cache_write', 'Cache write'],
  ['cache_read', 'Cache read'],
  ['output', 'Output'],
] as const

const PART_COLORS = ['bg-accent', 'bg-mint', 'bg-amber-500', 'bg-sky-500'] as const

type Part = (typeof PARTS)[number][0]
type Counts = Record<`${Part}_tokens`, number>

/** One row of the breakdown: a project, agent, thread, bot, model or call type. */
export type Item = {
  id: string
  name: string
  sub?: string
  href?: string
  title?: string
  parts: number[] // input, cache write, cache read, output tokens
  partCosts?: number[]
  cacheHit: number
  cost: number
  costLabel?: string // shown instead of the cost, e.g. "sub"
  unpriced?: string // why the cost is incomplete
  children?: Item[]
}

const partsOf = (counts: Counts) => PARTS.map(([part]) => counts[`${part}_tokens`])
const sum = (values: number[]) => values.reduce((a, b) => a + b, 0)

function bucketItem(bucket: CostBucket, extra: Partial<Item> = {}): Item {
  const input = bucket.input_tokens + bucket.cache_write_tokens + bucket.cache_read_tokens
  return {
    id: bucket.name,
    name: bucket.name,
    sub: `${bucket.calls.toLocaleString()} call${bucket.calls === 1 ? '' : 's'}${bucket.subscription_calls ? ` · ${bucket.subscription_calls} on a subscription` : ''}`,
    parts: partsOf(bucket),
    partCosts: PARTS.map(([part]) => bucket[`${part}_cost`]),
    cacheHit: input ? bucket.cache_read_tokens / input : 0,
    cost: bucket.cost,
    unpriced: bucket.unpriced_calls ? `${bucket.unpriced_calls} calls with no price count as $0` : undefined,
    ...extra,
  }
}

function threadItem(row: UsageThread): Item {
  return {
    id: row.id,
    name: row.title,
    sub: [row.bot, row.model, row.role === 'thread' ? 'thread' : 'main chat'].filter(Boolean).join(' · '),
    href: `/s/${row.id}`,
    parts: partsOf(row),
    cacheHit: row.cache_hit,
    cost: row.cost,
    costLabel: row.subscription ? 'sub' : undefined,
  }
}

function agentItem(row: AgentUsage, withProject = true): Item {
  const where = withProject ? row.project || 'no project' : row.branch
  return {
    id: `${row.worker_id}-${row.model}`,
    name: row.worker_title,
    sub: [where, row.agent, row.model, row.worker_status].filter(Boolean).join(' · '),
    href: `/s/${row.chat_id}`,
    title: `${row.chat_title} · ${row.requests.toLocaleString()} requests`,
    parts: partsOf(row),
    cacheHit: row.cache_hit,
    cost: row.cost,
    unpriced: row.priced ? undefined : `No list price for ${row.model}`,
  }
}

export function projectItems(data: Costs): Item[] {
  return data.agents.by_project.map((project: AgentProject) => {
    const owner = project.remote
      .replace(/^\w+:\/\/[^/]+\//, '')
      .split('/')
      .slice(0, -1)
      .join('/')
    return {
      id: project.key || 'none',
      name: project.name,
      title: project.remote || undefined,
      sub: [`${project.workers} agent${project.workers === 1 ? '' : 's'}`, project.agents.join(', '), owner]
        .filter(Boolean)
        .join(' · '),
      parts: partsOf(project),
      cacheHit: project.cache_hit,
      cost: project.cost,
      unpriced: project.unpriced_models.length ? `No list price for ${project.unpriced_models.join(', ')}` : undefined,
      children: data.agents.rows.filter(row => row.project_key === project.key).map(row => agentItem(row, false)),
    }
  })
}

function groupThreads(threads: UsageThread[], by: 'bot' | 'model'): Item[] {
  const groups = new Map<string, UsageThread[]>()
  for (const row of threads) groups.set(row[by], [...(groups.get(row[by]) ?? []), row])
  return [...groups].map(([name, rows]) => {
    const parts = PARTS.map(([part]) => sum(rows.map(row => row[`${part}_tokens`])))
    const input = parts[0] + parts[1] + parts[2]
    return {
      id: name,
      name,
      sub: `${rows.length} thread${rows.length === 1 ? '' : 's'}`,
      parts,
      cacheHit: input ? parts[2] / input : 0,
      cost: sum(rows.map(row => row.cost)),
      costLabel: rows.every(row => row.subscription) ? 'sub' : undefined,
      children: rows.map(threadItem),
    }
  })
}

export const VIEWS = ['Projects', 'Agents', 'Threads', 'Bots', 'Models', 'Call types'] as const
export type View = (typeof VIEWS)[number]

export function itemsFor(view: View, data: Costs): Item[] {
  switch (view) {
    case 'Projects':
      return projectItems(data)
    case 'Agents':
      return data.agents.rows.map(row => agentItem(row))
    case 'Threads':
      return data.usage.threads.map(threadItem)
    case 'Bots':
      return groupThreads(data.usage.threads, 'bot')
    case 'Models':
      return data.by_model.map(bucket => bucketItem(bucket))
    case 'Call types':
      return data.by_kind.map(bucket =>
        bucketItem(
          bucket,
          bucket.name === 'chat_reply'
            ? { children: data.chat_reply_by_bot.map(bot => bucketItem(bot, { id: `bot-${bot.name}` })) }
            : {},
        ),
      )
  }
}

const EMPTY: Record<View, string> = {
  Projects: 'No agent work in this period.',
  Agents: 'No agent sessions in this period.',
  Threads: 'No session calls in this period.',
  Bots: 'No session calls in this period.',
  Models: 'No calls in this period.',
  'Call types': 'No calls in this period.',
}

const NOTES: Partial<Record<View, string>> = {
  Projects:
    'Agents run on subscriptions; cost is what their tokens would cost at list price. A project is every worktree of one git remote.',
  Agents: 'Agents run on subscriptions; cost is what their tokens would cost at list price.',
}

function Stat({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="min-w-0 bg-surface px-4 py-3">
      <div className="truncate text-xs font-medium text-muted">{label}</div>
      <div className="mt-0.5 font-display text-xl font-semibold tabular-nums sm:text-2xl">{value}</div>
      {note && <div className="truncate text-xs text-muted">{note}</div>}
    </div>
  )
}

// Daily spend; exact values on hover.
function Daily({ days }: { days: Costs['by_day'] }) {
  const [hover, setHover] = useState<number | null>(null)
  const max = Math.max(...days.map(d => d.cost), 0.0001)
  const shown = hover != null ? days[hover] : null
  return (
    <div className="min-w-0 flex-1">
      <div className="flex items-baseline justify-between gap-2 text-xs text-muted">
        <span className="font-semibold text-ink">Daily bot spend</span>
        <span className="tabular-nums">
          {shown ? `${shown.date} · ${money(shown.cost)} · ${shown.calls} calls` : `peak ${money(max)}`}
        </span>
      </div>
      <div className="mt-2 flex h-16 items-end gap-[2px]" onMouseLeave={() => setHover(null)}>
        {days.map((d, i) => (
          <div key={d.date} className="flex h-full flex-1 items-end" onMouseEnter={() => setHover(i)}>
            <div
              className={`w-full rounded-t-sm ${hover === i ? 'bg-mint' : 'bg-accent'}`}
              style={{ height: d.cost ? `${Math.max(3, (d.cost / max) * 100)}%` : 0 }}
            />
          </div>
        ))}
      </div>
      <div className="mt-1 flex justify-between border-t border-stroke pt-1 text-[11px] text-muted">
        <span>{days[0]?.date}</span>
        <span>{days[days.length - 1]?.date}</span>
      </div>
    </div>
  )
}

function Legend() {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted" aria-hidden="true">
      {PARTS.map(([part, label], i) => (
        <span key={part} className="inline-flex items-center gap-1.5">
          <span className={`h-2 w-2 rounded-sm ${PART_COLORS[i]}`} />
          {label}
        </span>
      ))}
    </div>
  )
}

/** A thin bar: its length is the row's share of the largest row, its colors the token mix. */
function MixBar({ item, max }: { item: Item; max: number }) {
  const total = sum(item.parts)
  const label = PARTS.map(
    ([, name], i) =>
      `${name} ${tokens(item.parts[i])}${item.partCosts && item.parts[i] ? ` (${money(item.partCosts[i])})` : ''}`,
  ).join(' · ')
  return (
    <div className="mt-1.5 h-1.5 rounded-full bg-raised" title={label}>
      <div className="flex h-full overflow-hidden rounded-full" style={{ width: `${max ? (100 * total) / max : 0}%` }}>
        {item.parts.map(
          (count, i) =>
            count > 0 && <div key={i} className={PART_COLORS[i]} style={{ width: `${(100 * count) / total}%` }} />,
        )}
      </div>
    </div>
  )
}

function Cost({ item }: { item: Item }) {
  if (item.costLabel) return <span className="text-muted">{item.costLabel}</span>
  return (
    <>
      {money(item.cost)}
      {item.unpriced && (
        <span className="ml-0.5 text-warning" title={item.unpriced}>
          *
        </span>
      )}
    </>
  )
}

function ItemRow({ item, max, depth = 0, nested }: { item: Item; max: number; depth?: number; nested: boolean }) {
  const [open, setOpen] = useState(false)
  const kids = item.children ?? []
  const name = item.href ? (
    <Link to={item.href} className="hover:text-accent hover:underline" title={item.title}>
      {item.name}
    </Link>
  ) : (
    <span title={item.title}>{item.name}</span>
  )
  return (
    <>
      <li
        className={`grid grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-x-4 px-4 py-2.5 sm:grid-cols-[minmax(0,1fr)_5rem_4rem_5rem] ${depth ? 'bg-raised/40' : ''}`}
      >
        <div className="flex min-w-0 items-start gap-2" style={{ paddingLeft: depth * 20 }}>
          {kids.length > 0 ? (
            <button
              type="button"
              onClick={() => setOpen(!open)}
              aria-expanded={open}
              aria-label={`${open ? 'Hide' : 'Show'} ${item.name}'s ${kids.length} rows`}
              className="mt-0.5 w-4 shrink-0 text-xs text-muted hover:text-ink"
            >
              {open ? '▾' : '▸'}
            </button>
          ) : (
            nested && <span className="w-4 shrink-0" />
          )}
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-medium">{name}</div>
            {item.sub && <div className="truncate text-xs text-muted">{item.sub}</div>}
            <MixBar item={item} max={max} />
          </div>
        </div>
        <div className="text-right text-sm tabular-nums">{tokens(sum(item.parts))}</div>
        <div className="hidden text-right text-sm tabular-nums text-muted sm:block">{percent(item.cacheHit)}</div>
        <div className="text-right text-sm font-medium tabular-nums">
          <Cost item={item} />
        </div>
      </li>
      {open && kids.map(kid => <ItemRow key={kid.id} item={kid} max={max} depth={depth + 1} nested />)}
    </>
  )
}

const SORTS = { tokens: 'Tokens', cost: 'Cost', cacheHit: 'Cache hit' } as const
type Sort = keyof typeof SORTS
const SHOWN = 8

export function Breakdown({ data, view, onView }: { data: Costs; view: View; onView: (view: View) => void }) {
  const [sort, setSort] = useState<Sort>('tokens')
  const [all, setAll] = useState(false)
  const key = (item: Item) => (sort === 'tokens' ? sum(item.parts) : item[sort])
  const items = itemsFor(view, data).sort((a, b) => key(b) - key(a) || a.name.localeCompare(b.name))
  const max = Math.max(0, ...items.map(item => sum(item.parts)))
  const shown = all ? items : items.slice(0, SHOWN)
  const nested = items.some(item => item.children?.length)
  const header = (sortKey: Sort, className = '') => (
    <button
      type="button"
      onClick={() => setSort(sortKey)}
      aria-pressed={sort === sortKey}
      className={`text-right hover:text-ink ${sort === sortKey ? 'font-semibold text-ink' : ''} ${className}`}
    >
      {SORTS[sortKey]}
      {sort === sortKey ? ' ↓' : ''}
    </button>
  )
  return (
    <section className="surface-card mt-4 overflow-hidden" aria-label="Breakdown">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-stroke px-4 pt-3">
        <div
          className="-mb-px flex gap-1 overflow-x-auto"
          style={{ scrollbarWidth: 'none' }}
          role="tablist"
          aria-label="Break down by"
        >
          {VIEWS.map(option => (
            <button
              key={option}
              type="button"
              role="tab"
              aria-selected={view === option}
              onClick={() => {
                onView(option)
                setAll(false)
              }}
              className={`whitespace-nowrap border-b-2 px-3 pb-2.5 text-sm ${view === option ? 'border-accent font-semibold text-ink' : 'border-transparent text-muted hover:text-ink'}`}
            >
              {option}
            </button>
          ))}
        </div>
        <div className="pb-2.5">
          <Legend />
        </div>
      </div>
      <div className="grid grid-cols-[minmax(0,1fr)_auto_auto] gap-x-4 border-b border-stroke px-4 py-2 text-xs text-muted sm:grid-cols-[minmax(0,1fr)_5rem_4rem_5rem]">
        <span className={nested ? 'pl-6' : ''}>
          {items.length} {items.length === 1 ? 'row' : 'rows'}
        </span>
        {header('tokens')}
        {header('cacheHit', 'hidden sm:block')}
        {header('cost')}
      </div>
      <ul className="divide-y divide-stroke">
        {shown.map(item => (
          <ItemRow key={`${view}-${item.id}`} item={item} max={max} nested={nested} />
        ))}
        {!items.length && <li className="px-4 py-6 text-sm text-muted">{EMPTY[view]}</li>}
      </ul>
      {items.length > SHOWN && (
        <button
          type="button"
          onClick={() => setAll(!all)}
          className="w-full border-t border-stroke px-4 py-2.5 text-sm text-accent hover:bg-raised"
        >
          {all ? 'Show fewer' : `Show all ${items.length}`}
        </button>
      )}
      {NOTES[view] && <p className="border-t border-stroke px-4 py-2.5 text-xs text-muted">{NOTES[view]}</p>}
    </section>
  )
}

export function Summary({ data }: { data: Costs }) {
  const { total, usage, agents } = data
  return (
    <div className="mt-6 grid grid-cols-2 gap-px overflow-hidden rounded-card border border-stroke bg-stroke lg:grid-cols-5 max-lg:[&>*:last-child]:col-span-2">
      <Stat
        label="Bot spend"
        value={money(total.cost)}
        note={`${total.calls.toLocaleString()} calls · ${data.days} days`}
      />
      <Stat
        label="Bot tokens"
        value={tokens(usage.headline.tokens)}
        note={`${percent(usage.headline.cache_hit)} from cache`}
      />
      <Stat label="On subscription" value={percent(usage.headline.subscription)} note="of bot tokens" />
      <Stat
        label="Agents"
        value={agents.headline.sessions.toLocaleString()}
        note={`${agents.headline.projects} project${agents.headline.projects === 1 ? '' : 's'} · ${tokens(agents.headline.tokens)} tokens`}
      />
      <Stat label="Agent list price" value={money(agents.headline.cost)} note="on subscriptions" />
    </div>
  )
}

export function CostsBody({
  data,
  view,
  onView,
  dim = false,
}: {
  data: Costs
  view: View
  onView: (view: View) => void
  dim?: boolean
}) {
  return (
    <div className={dim ? 'opacity-60' : ''}>
      <Summary data={data} />
      <div className="surface-card mt-4 px-4 py-3">
        <Daily days={data.by_day} />
      </div>
      <Breakdown data={data} view={view} onView={onView} />
      {!!data.unpriced_models.length && (
        <p className="mt-3 text-xs text-warning">
          * No price for {data.unpriced_models.join(', ')}; those calls count as $0. Add prices under
          DJANGO_ERGO["MODEL_PRICES"].
        </p>
      )}
      <p className="mt-2 text-xs text-muted">
        Costs are estimates from token counts and list prices, each part at the model's own rate. Hover a bar for its
        token mix and what each part cost.
      </p>
    </div>
  )
}

const VIEW_KEY = 'ergonaut.costs.view'

function savedView(): View {
  try {
    const saved = localStorage.getItem(VIEW_KEY)
    return (VIEWS as readonly string[]).includes(saved ?? '') ? (saved as View) : 'Projects'
  } catch {
    return 'Projects'
  }
}

export function CostsPage() {
  const [days, setDays] = useState(7)
  const [data, setData] = useState<Costs | null>(null)
  const [error, setError] = useState('')
  const [bot, setBot] = useState('')
  const [bots, setBots] = useState<string[]>([])
  const [view, setView] = useState<View>(savedView)

  useEffect(() => {
    api
      .bots()
      .then(items => setBots(items.map(item => item.name)))
      .catch(() => {})
  }, [])

  useEffect(() => {
    let active = true
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

  const choose = (next: View) => {
    setView(next)
    try {
      localStorage.setItem(VIEW_KEY, next)
    } catch {
      // A remembered tab is a convenience.
    }
  }

  return (
    <div className="page-content h-full overflow-y-auto">
      <div className="flex flex-wrap items-end gap-4">
        <div>
          <p className="eyebrow">Usage</p>
          <h1 className="page-title mt-3">Costs &amp; usage</h1>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <select
            aria-label="Filter by bot"
            value={bot}
            onChange={e => setBot(e.target.value)}
            className="rounded-control border border-stroke bg-surface px-3 py-1.5 text-sm"
          >
            <option value="">All bots</option>
            {bots.map(name => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
          <div className="flex gap-0.5 rounded-control border border-stroke bg-surface p-0.5" aria-label="Period">
            {RANGES.map(r => (
              <button
                key={r}
                type="button"
                onClick={() => setDays(r)}
                aria-pressed={r === days}
                className={`rounded-control px-3 py-1 text-sm ${r === days ? 'bg-accent font-semibold text-canvas' : 'text-muted hover:bg-raised'}`}
              >
                {r}d
              </button>
            ))}
          </div>
        </div>
      </div>

      {!data ? (
        <div className="mt-6 text-sm text-muted">{error || 'Loading…'}</div>
      ) : (
        <CostsBody data={data} view={view} onView={choose} dim={!!error} />
      )}
    </div>
  )
}
