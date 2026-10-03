import { Fragment, useEffect, useState } from 'react'
import type { CostBucket, Costs } from '../api'
import { api } from '../api'

const RANGES = [7, 30, 90]

function money(value: number) {
  if (value === 0) return '$0'
  if (value < 0.0001) return '<$0.0001'
  // Cheap models cost fractions of a cent per call; keep two significant digits.
  if (value < 0.01) return `$${value.toPrecision(2)}`
  return value.toLocaleString(undefined, {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: value < 10 ? 2 : 0,
  })
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

// One series (cost per day): a single hue, no legend; hover shows the day.
function Daily({ days }: { days: Costs['by_day'] }) {
  const [hover, setHover] = useState<number | null>(null)
  const max = Math.max(...days.map(d => d.cost), 0.0001)
  const shown = hover != null ? days[hover] : null
  return (
    <div>
      <div className="mb-1 h-5 text-xs text-zinc-500">
        {shown ? `${shown.date}: ${money(shown.cost)} · ${shown.calls} calls` : 'Cost per day'}
      </div>
      <div
        className="flex h-32 items-end gap-[2px] border-b border-zinc-200 dark:border-zinc-800"
        onMouseLeave={() => setHover(null)}
      >
        {days.map((d, i) => (
          <div key={d.date} className="flex h-full flex-1 items-end" onMouseEnter={() => setHover(i)}>
            <div
              className={`w-full rounded-t ${hover === i ? 'bg-indigo-700 dark:bg-indigo-300' : 'bg-indigo-500 dark:bg-indigo-400'}`}
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
      <div className="rounded-card border border-stroke overflow-x-auto">
      <table className="w-full max-w-4xl text-sm">
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
  const [days, setDays] = useState(30)
  const [data, setData] = useState<Costs | null>(null)
  const [error, setError] = useState('')
  const [open, setOpen] = useState(true)

  useEffect(() => {
    api
      .costs(days)
      .then(setData)
      .catch(e => setError(String(e.message ?? e)))
  }, [days])

  if (!data) return <div className="p-6 text-zinc-500">{error || 'Loading…'}</div>

  const { total } = data
  return (
    <div className="page-content h-full overflow-y-auto">
      <div className="flex items-center gap-3">
        <h1 className="page-title">Costs</h1>
        <div className="ml-auto flex gap-1 rounded-card border border-stroke bg-surface p-1">
          {RANGES.map(r => (
            <button
              key={r}
              onClick={() => setDays(r)}
              className={`rounded-control px-4 py-2 text-sm ${r === days ? 'bg-indigo-tint font-medium text-accent-soft' : 'hover:bg-raised'}`}
            >
              {r} days
            </button>
          ))}
        </div>
      </div>

      <div className="mt-6 grid max-w-4xl grid-cols-1 gap-4 sm:grid-cols-3">
        <Tile label={`Spent, last ${data.days} days`} value={money(total.cost)} />
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
