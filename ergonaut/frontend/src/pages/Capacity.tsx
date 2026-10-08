import type { Capacity, CapacityAccount, CapacitySync, CapacityWindow } from '../api'
import { agoLong } from '../time'

// Provider limits for the Routing page: how healthy the last sync was, then each account's 5-hour and
// 7-day windows. A value is only ever shown with the time it was observed once it is old, and a window
// the provider didn't report is "Unavailable", never 0%.

const epochIso = (seconds: number) => new Date(seconds * 1000).toISOString()

/** "2h 14m", "3d 8h", "under a minute", from a reset time (epoch seconds). */
export function countdown(resetsAt: number, now = Date.now()): string {
  const minutes = Math.max(0, Math.floor((resetsAt * 1000 - now) / 60000))
  if (minutes < 1) return 'under a minute'
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ${String(minutes % 60).padStart(2, '0')}m`
  return `${Math.floor(hours / 24)}d ${String(hours % 24).padStart(2, '0')}h`
}

type Row = { label: string; window: CapacityWindow | null; note: string }

const PLAIN: Record<string, string> = { five_hour: '5-hour window', weekly: '7-day window' }

/** One row per reported window; a 5-hour and a 7-day row are always there, so a missing one shows as unavailable. */
export function rowsFor(account: CapacityAccount): Row[] {
  const absent =
    account.status === 'error'
      ? 'Sync failed'
      : account.status === 'unavailable'
        ? 'No data yet'
        : 'Not reported by provider'
  const rows = (period: string, fallback: string): Row[] => {
    const found = account.windows.filter(w => w.period === period)
    return found.length
      ? found.map(w => ({ label: PLAIN[w.key] ?? w.label, window: w, note: '' }))
      : [{ label: fallback, window: null, note: absent }]
  }
  const other = account.windows
    .filter(w => w.period !== '5h' && w.period !== '7d')
    .map(w => ({ label: w.label, window: w, note: '' }))
  return [...rows('5h', '5-hour window'), ...rows('7d', '7-day window'), ...other]
}

const SYNC: Record<CapacitySync['state'], { icon: string; title: string; tone: string }> = {
  healthy: { icon: '●', title: 'Source sync healthy', tone: 'text-teal' },
  stale: { icon: '▲', title: 'Limits are stale', tone: 'text-warning' },
  partial: { icon: '▲', title: 'Some providers failed to sync', tone: 'text-warning' },
  failed: { icon: '✕', title: 'Source sync failed', tone: 'text-danger' },
  empty: { icon: '○', title: 'No limits synced yet', tone: 'text-muted' },
}

function minutes(seconds: number) {
  const m = Math.round(seconds / 60)
  return `${m} minute${m === 1 ? '' : 's'}`
}

export function SyncBar({
  sync,
  refreshing,
  refreshError,
  onRefresh,
  now,
}: {
  sync: CapacitySync
  refreshing: boolean
  refreshError: string
  onRefresh: () => void
  now: number
}) {
  const busy = refreshing || sync.running
  const look = busy ? { icon: '↻', title: 'Syncing limits…', tone: 'text-accent-soft' } : SYNC[sync.state]
  const last = sync.succeeded_at ? (
    <>
      Last successful sync · <time dateTime={sync.succeeded_at}>{agoLong(sync.succeeded_at, now)}</time> /{' '}
      {new Date(sync.succeeded_at).toISOString().slice(11, 16)} UTC
    </>
  ) : (
    'No successful sync yet'
  )
  const detail: Record<CapacitySync['state'], string> = {
    healthy: '',
    stale: `Values are older than ${minutes(sync.stale_after)} and may no longer match the provider.`,
    partial: 'Providers that failed keep their last values, marked stale below.',
    failed: sync.error ? `Last attempt failed: ${sync.error}.` : 'The last attempt failed.',
    empty: 'Refresh limits to fetch them now. The periodic sync fills this in too.',
  }
  return (
    <section
      aria-labelledby="capacity-sync"
      className="surface-card flex flex-wrap items-center gap-x-6 gap-y-3 p-4 sm:px-5"
    >
      <div className="min-w-0 flex-1" aria-live="polite" aria-atomic="true">
        <h2 id="capacity-sync" className={`flex items-center gap-2 text-sm font-semibold ${look.tone}`}>
          <span aria-hidden="true">{look.icon}</span>
          <span className="text-ink">{look.title}</span>
        </h2>
        <p className="mt-1 text-xs text-muted">{last}</p>
        {detail[sync.state] && !busy && <p className="mt-1 text-xs text-muted">{detail[sync.state]}</p>}
        {refreshError && !busy && <p className="mt-1 text-xs text-danger">Refresh failed: {refreshError}</p>}
      </div>
      <button
        type="button"
        aria-disabled={busy}
        onClick={() => !busy && onRefresh()}
        className="inline-flex min-h-11 items-center gap-2 rounded-control bg-accent px-4 text-sm font-semibold text-canvas aria-disabled:cursor-wait aria-disabled:opacity-60"
      >
        {busy ? 'Refreshing…' : 'Refresh limits'}
        <span aria-hidden="true" className={busy ? 'inline-block motion-safe:animate-spin' : ''}>
          ↻
        </span>
      </button>
    </section>
  )
}

function tone(w: CapacityWindow, used: number) {
  if (w.limit != null && used >= w.limit) return { fill: 'bg-danger', note: 'Over the router limit' }
  if (w.limit != null && used >= w.limit - 15) return { fill: 'bg-warning', note: 'Near the router limit' }
  return { fill: w.period === '7d' ? 'bg-accent' : 'bg-mint', note: '' }
}

function WindowRow({ row, now }: { row: Row; now: number }) {
  const w = row.window
  const used = w?.used ?? null
  const reset = w?.status === 'reset'
  const unavailable = used == null
  const note = reset ? 'Window reset, waiting for a fresh sync' : row.note
  const level = used != null && w ? tone(w, used) : null
  const resets = w?.resets_at
    ? `Resets in ${countdown(w.resets_at, now)}`
    : w && !unavailable
      ? 'Reset time unavailable'
      : ''
  return (
    <li className={w?.stale && !unavailable ? 'opacity-90' : ''}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span className="text-sm font-medium text-muted">
          {row.label}
          {w?.model && <span className="font-normal"> · {w.model} only</span>}
        </span>
        {w?.stale && !unavailable && (
          <span className="inline-flex items-center gap-1 rounded-full bg-amber-tint px-2 py-0.5 text-xs font-semibold text-warning">
            <span aria-hidden="true">▲</span>
            Stale
            {w.observed_at ? (
              <>
                <span className="font-normal">· as of</span>
                <time dateTime={epochIso(w.observed_at)} title={new Date(w.observed_at * 1000).toLocaleString()}>
                  {agoLong(epochIso(w.observed_at), now)}
                </time>
              </>
            ) : (
              <span className="font-normal">· age unknown</span>
            )}
          </span>
        )}
      </div>
      <div className="mt-1 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        {unavailable ? (
          <span className="font-display text-2xl font-semibold text-muted">
            <span aria-hidden="true">— · </span>Unavailable
          </span>
        ) : (
          <span className="font-display text-2xl font-semibold tabular-nums">
            {Math.round(used)}% used
            <span className="ml-2 font-sans text-sm font-normal text-muted">{Math.round(100 - used)}% left</span>
          </span>
        )}
        <span className="text-xs text-muted">
          {unavailable ? (
            note
          ) : (
            <time
              dateTime={w!.resets_at ? epochIso(w!.resets_at) : undefined}
              title={w!.resets_at ? new Date(w!.resets_at * 1000).toUTCString() : undefined}
            >
              {resets}
            </time>
          )}
        </span>
      </div>
      {unavailable ? (
        <div className="mt-2 h-2.5 rounded-full bg-raised" aria-hidden="true" />
      ) : (
        <div
          className={`relative mt-2 h-2.5 rounded-full bg-raised ${w!.stale ? 'opacity-60' : ''}`}
          role="meter"
          aria-label={`${row.label} usage`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(used)}
          aria-valuetext={`${Math.round(used)}% used, ${Math.round(100 - used)}% left${resets ? `, ${resets.toLowerCase()}` : ''}${w!.stale ? ', stale' : ''}`}
        >
          <div
            className={`absolute inset-y-0 left-0 rounded-full ${level!.fill}`}
            style={{ width: `${Math.max(0, Math.min(100, used))}%` }}
          />
          {w!.limit != null && (
            <div
              className="absolute -inset-y-1 w-0.5 rounded bg-ink"
              style={{ left: `${Math.min(99.5, w!.limit)}%` }}
              title={`Router skips this provider at ${w!.limit}% used`}
            />
          )}
        </div>
      )}
      {(level?.note || (w?.limit != null && !unavailable)) && (
        <p className="mt-1 text-xs text-muted">
          {level?.note && <span className="font-semibold text-warning">{level.note} · </span>}
          {w?.limit != null && `Router skips at ${w.limit}% used`}
        </p>
      )}
    </li>
  )
}

const BADGE: Record<CapacityAccount['status'], { icon: string; text: string; cls: string }> = {
  ok: { icon: '●', text: 'Connected', cls: 'bg-teal-tint text-teal' },
  stale: { icon: '▲', text: 'Stale', cls: 'bg-amber-tint text-warning' },
  error: { icon: '✕', text: 'Sync failed', cls: 'bg-red-tint text-danger' },
  unavailable: { icon: '○', text: 'No data', cls: 'bg-raised text-muted' },
}

export function AccountCard({ account, now }: { account: CapacityAccount; now: number }) {
  const badge = BADGE[account.status]
  const titleId = `capacity-${account.id}`
  return (
    <article aria-labelledby={titleId} className="surface-card flex flex-col gap-4 p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 id={titleId} className="font-display text-lg font-semibold">
            {account.name}
          </h3>
          <p className="text-xs text-muted">
            {account.providers.length
              ? `Routes ${account.providers.join(', ')}`
              : 'Shown for reference, not used for routing'}
          </p>
        </div>
        <span
          className={`inline-flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold ${badge.cls}`}
        >
          <span aria-hidden="true">{badge.icon}</span>
          {badge.text}
        </span>
      </div>
      {account.error && (
        <p className="text-sm text-danger">
          <span aria-hidden="true">✕ </span>This provider's last sync failed: {account.error}
          {account.windows.length > 0 && '. The values below are its last known ones.'}
        </p>
      )}
      <ul className="flex flex-col gap-5 border-t border-stroke pt-4">
        {rowsFor(account).map(row => (
          <WindowRow key={row.window?.key ?? row.label} row={row} now={now} />
        ))}
      </ul>
      {account.fetched_at && (
        <p className="mt-auto border-t border-stroke pt-3 text-xs text-muted">
          Provider data fetched <time dateTime={account.fetched_at}>{agoLong(account.fetched_at, now)}</time> /{' '}
          {new Date(account.fetched_at).toISOString().slice(11, 16)} UTC
        </p>
      )}
    </article>
  )
}

export function CapacitySection({
  capacity,
  refreshing,
  refreshError,
  onRefresh,
  now = Date.now(),
}: {
  capacity: Capacity
  refreshing: boolean
  refreshError: string
  onRefresh: () => void
  now?: number
}) {
  const { sync, accounts } = capacity
  return (
    <div className="flex flex-col gap-6">
      <SyncBar sync={sync} refreshing={refreshing} refreshError={refreshError} onRefresh={onRefresh} now={now} />
      <section aria-labelledby="capacity-windows">
        <h2 id="capacity-windows" className="font-display text-2xl font-semibold">
          Provider windows
        </h2>
        <p className="mt-1 text-sm text-muted">
          Window usage is shown with its reset time. Missing data is explicit, never read as zero.
        </p>
        {accounts.length ? (
          <div className="mt-4 grid gap-4 md:grid-cols-2">
            {accounts.map(account => (
              <AccountCard key={account.id} account={account} now={now} />
            ))}
          </div>
        ) : (
          <p className="surface-card mt-4 p-5 text-sm text-muted">
            No subscription providers are configured and no limits have been synced. Add a <code>transport: cli</code>{' '}
            provider to providers.yaml, or press Refresh limits to read the accounts <code>omp</code> is signed in to.
          </p>
        )}
      </section>
      <section aria-labelledby="capacity-notes" className="surface-card p-5">
        <h2 id="capacity-notes" className="text-sm font-semibold">
          How to read this
        </h2>
        <p className="mt-2 text-sm text-muted">
          Usage percentages are provider-reported. A window that is missing or failed to sync shows as Unavailable. A
          value older than {minutes(sync.stale_after)} keeps its last known number, flagged Stale with the time it was
          observed, and is never carried forward as current. Limits sync every few minutes; Refresh limits fetches them
          now.
        </p>
      </section>
    </div>
  )
}

/** Shown until the first response arrives. */
export function CapacityLoading() {
  return (
    <div aria-busy="true" className="flex flex-col gap-6">
      <p role="status" className="text-sm text-muted">
        Loading limits…
      </p>
      <div className="surface-card h-20 animate-pulse" aria-hidden="true" />
      <div className="grid gap-4 md:grid-cols-2" aria-hidden="true">
        <div className="surface-card h-72 animate-pulse" />
        <div className="surface-card h-72 animate-pulse" />
      </div>
    </div>
  )
}
