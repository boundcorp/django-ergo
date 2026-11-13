import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import type { Routing, RoutingProvider, RoutingWindow } from '../api'
import { api } from '../api'
import { pace } from '../usagePace'
export { pace, windowSeconds } from '../usagePace'
import { useDismissablePopover } from './ShellControls'

// The top bar summarizes the tightest reported limit; the popover has every window.
// Data comes from GET /api/routing, which already carries each provider's windows.

const HOUR = 3600
const DAY = 24 * HOUR
const POLL_MS = 60_000

// The short tag shown under a ring: 5h, wk, Fable, 30m.
export function shortTag(key: string, w?: RoutingWindow): string {
  if (key === 'five_hour') return '5h'
  if (key === 'weekly') return 'wk'
  if (key.startsWith('weekly_')) {
    const model = w?.model || key.slice('weekly_'.length)
    return model.charAt(0).toUpperCase() + model.slice(1)
  }
  const minutes = /^minutes_(\d+)$/.exec(key)
  if (minutes) return `${minutes[1]}m`
  return (w?.label || key).replace(/[_-]+/g, ' ')
}

export function longLabel(key: string, w?: RoutingWindow): string {
  if (key === 'five_hour') return '5-hour'
  if (key === 'weekly') return 'Weekly'
  if (key.startsWith('weekly_')) return `Weekly · ${shortTag(key, w)}`
  const label = w?.label || key.replace(/[_-]+/g, ' ')
  return label.charAt(0).toUpperCase() + label.slice(1)
}

// "38m", "2h 38m", "3d 16h".
export function countdown(resetsAt: number | null, now = Date.now() / 1000): string {
  if (!resetsAt) return ''
  const left = Math.max(0, Math.round(resetsAt - now))
  if (left < HOUR) return `${Math.max(1, Math.round(left / 60))}m`
  if (left < DAY) return `${Math.floor(left / HOUR)}h ${Math.floor((left % HOUR) / 60)}m`
  return `${Math.floor(left / DAY)}d ${Math.floor((left % DAY) / HOUR)}h`
}

// Merge account-level capacity with configured routes without showing an account twice.
function usageProviders(data: Routing): RoutingProvider[] {
  const matched = new Set<string>()
  const accounts = data.capacity.accounts.map(account => {
    const routes = data.providers.filter(p => account.providers.includes(p.name))
    routes.forEach(p => matched.add(p.name))
    const route = routes[0]
    const windows: Record<string, RoutingWindow> = {}
    for (const w of account.windows) {
      windows[w.key] = {
        ...w,
        limit: routes.find(p => p.windows[w.key])?.windows[w.key].limit ?? 100,
      }
    }
    for (const p of routes) {
      for (const [key, window] of Object.entries(p.windows)) {
        if (!(key in windows)) windows[key] = window
      }
    }
    return {
      name: account.name,
      type: route?.type ?? account.id,
      transport: route?.transport ?? '',
      subscription: true,
      api_key_env: '',
      status: route?.status ?? 'standby',
      reason: account.status === 'error' ? 'Sync failed' : account.status === 'unavailable' ? 'Unavailable' : '',
      windows,
      reported_at: account.fetched_at,
      stale:
        Object.keys(windows).length > 0 &&
        (account.status === 'stale' || account.status === 'error' || account.windows.some(w => w.stale)),
    } satisfies RoutingProvider
  })
  return [...accounts, ...data.providers.filter(p => !matched.has(p.name))]
}

export type Tone = 'ok' | 'warn' | 'over' | 'none'

// Same thresholds as the Routing page: past the routing limit, or within 15 points of it.
export function tone(w: RoutingWindow): Tone {
  if (w.used == null) return 'none'
  if (w.used >= w.limit) return 'over'
  if (w.used >= w.limit - 15) return 'warn'
  return 'ok'
}

type Entry = { provider: RoutingProvider; key: string; window: RoutingWindow }

const reported = (e: Entry) => e.window.used != null

// The window closest to its routing limit, across all reported providers.
export function tightest(providers: RoutingProvider[]): Entry | null {
  let best: Entry | null = null
  for (const provider of providers)
    for (const [key, window] of Object.entries(provider.windows)) {
      const entry = { provider, key, window }
      if (!reported(entry)) continue
      const headroom = (w: RoutingWindow) => w.limit - (w.used ?? 0)
      if (!best || headroom(window) < headroom(best.window)) best = entry
    }
  return best
}

function Glyph({ type }: { type: string }) {
  if (type === 'claude')
    return (
      <svg className="usage-glyph usage-glyph-claude" viewBox="0 0 16 16" aria-hidden="true">
        {[0, 45, 90, 135].map(angle => (
          <line
            key={angle}
            x1="8"
            y1="1.5"
            x2="8"
            y2="14.5"
            transform={`rotate(${angle} 8 8)`}
            stroke="currentColor"
            strokeWidth="2.2"
            strokeLinecap="round"
          />
        ))}
      </svg>
    )
  if (type === 'openai')
    return (
      <svg className="usage-glyph usage-glyph-openai" viewBox="0 0 16 16" aria-hidden="true">
        <path
          d="M8 1.5 13.6 4.75v6.5L8 14.5 2.4 11.25v-6.5Z"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinejoin="round"
        />
        <circle cx="8" cy="8" r="2" fill="currentColor" />
      </svg>
    )
  return (
    <svg className="usage-glyph" viewBox="0 0 16 16" aria-hidden="true">
      <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="1.8" />
    </svg>
  )
}

// A progress ring: the arc is the share used, the tick is the pace (window passed).
export function Ring({ k, w, size = 22, now }: { k: string; w: RoutingWindow; size?: number; now?: number }) {
  const r = 8
  const circumference = 2 * Math.PI * r
  const used = w.used == null ? 0 : Math.max(0, Math.min(100, w.used))
  const passed = pace(k, w, now)
  const angle = passed == null ? null : (passed / 100) * 2 * Math.PI
  return (
    <svg
      className={`usage-ring usage-tone-${tone(w)}`}
      width={size}
      height={size}
      viewBox="0 0 22 22"
      aria-hidden="true"
    >
      <circle
        className="usage-ring-track"
        cx="11"
        cy="11"
        r={r}
        fill="none"
        strokeWidth="3"
        strokeDasharray={w.used == null ? '2 2.4' : undefined}
      />
      {w.used != null && used > 0 && (
        <circle
          className="usage-ring-fill"
          cx="11"
          cy="11"
          r={r}
          fill="none"
          strokeWidth="3"
          strokeLinecap="round"
          strokeDasharray={`${(used / 100) * circumference} ${circumference}`}
          transform="rotate(-90 11 11)"
        />
      )}
      {angle != null && (
        <line
          className="usage-pace"
          x1={11 + Math.sin(angle) * 5}
          y1={11 - Math.cos(angle) * 5}
          x2={11 + Math.sin(angle) * 11}
          y2={11 - Math.cos(angle) * 11}
          strokeWidth="1.6"
          strokeLinecap="round"
        />
      )}
    </svg>
  )
}

function Bar({ k, w, now }: { k: string; w: RoutingWindow; now?: number }) {
  const passed = pace(k, w, now)
  const used = w.used ?? 0
  return (
    <div
      className={`usage-bar usage-tone-${tone(w)}`}
      role={w.used == null ? undefined : 'meter'}
      aria-hidden={w.used == null ? true : undefined}
      aria-label={`${longLabel(k, w)} usage`}
      aria-valuemin={w.used == null ? undefined : 0}
      aria-valuemax={w.used == null ? undefined : 100}
      aria-valuenow={w.used == null ? undefined : Math.max(0, Math.min(100, w.used))}
      aria-valuetext={w.used == null ? undefined : windowText(k, w, now)}
    >
      {w.used != null && <div className="usage-bar-fill" style={{ width: `${Math.max(0, Math.min(100, used))}%` }} />}
      {passed != null && <div className="usage-bar-pace" style={{ left: `${passed}%` }} />}
    </div>
  )
}

function windowText(k: string, w: RoutingWindow, now?: number): string {
  const label = longLabel(k, w)
  if (w.used == null) return `${label}: ${w.status === 'reset' ? 'reset, not reported since' : 'not reported'}`
  const passed = pace(k, w, now)
  return [
    `${label}: ${Math.round(w.used)}% used`,
    w.resets_at ? `resets in ${countdown(w.resets_at, now)}` : '',
    passed != null ? `${Math.round(passed)}% of the window has passed` : '',
  ]
    .filter(Boolean)
    .join(', ')
}

export function UsageTrigger({ providers, now }: { providers: RoutingProvider[]; now?: number }) {
  const tight = tightest(providers)
  if (!tight) return <span className="usage-limit-value">Usage –</span>
  const { provider, key, window } = tight
  return (
    <span className="usage-limit" title={`${provider.name}: ${windowText(key, window, now)}`}>
      <Ring k={key} w={window} now={now} />
      <span className="usage-limit-value">{Math.round(window.used!)}%</span>
      <span className="usage-limit-tag">{shortTag(key, window)}</span>
    </span>
  )
}

export function UsagePanel({
  data,
  now,
  onRefresh,
  refreshing = false,
  onNavigate,
}: {
  data: Routing
  now?: number
  onRefresh?: () => void
  refreshing?: boolean
  onNavigate?: () => void
}) {
  const providers = usageProviders(data)
  return (
    <>
      <div className="usage-head">
        <span className="font-semibold text-ink">Usage</span>
        <span className="text-xs text-muted">{providers.length} providers</span>
        {onRefresh && (
          <button
            type="button"
            className="usage-refresh"
            onClick={onRefresh}
            aria-label="Refresh usage"
            title="Refresh usage"
            disabled={refreshing}
          >
            <span aria-hidden="true" className={refreshing ? 'animate-spin' : ''}>
              ↻
            </span>
          </button>
        )}
      </div>
      {data.capacity.sync.state === 'failed' && (
        <p className="usage-reported text-warning" role="status">
          Usage sync failed{providers.some(p => Object.keys(p.windows).length > 0) && ' · showing last data'}
        </p>
      )}
      {!providers.length && <p className="usage-empty">No providers available.</p>}
      {providers.map((p, index) => {
        const windows = Object.entries(p.windows)
        return (
          <section key={`${p.name}-${index}`} className="usage-section" aria-label={`${p.name} usage`}>
            <div className="usage-section-head">
              <span className="usage-glyph-box">
                <Glyph type={p.type} />
              </span>
              <span className="font-semibold text-ink">{p.name}</span>
              {p.status === 'in_use' && <span className="usage-chip">In use</span>}
              {p.status === 'skipped' && <span className="usage-chip usage-chip-danger">Skipped</span>}
            </div>
            {windows.length ? (
              <ul className="usage-rows">
                {windows.map(([k, w]) => {
                  return (
                    <li key={k} className="usage-row" title={windowText(k, w, now)}>
                      <span className="usage-row-label">{longLabel(k, w)}</span>
                      <Bar k={k} w={w} now={now} />
                      <span className="usage-row-value">{w.used == null ? '–' : `${Math.round(w.used)}%`}</span>
                      <span className="usage-row-reset" title="Time until reset">
                        {w.resets_at && (
                          <>
                            <span className="sr-only">Resets in </span>
                            {countdown(w.resets_at, now)}
                          </>
                        )}
                      </span>
                      <span className="usage-row-meta">
                        {w.used == null
                          ? w.status === 'reset'
                            ? 'Reset · not reported since'
                            : 'Not reported'
                          : [
                              pace(k, w, now) != null && `${Math.round(pace(k, w, now)!)}% elapsed`,
                              w.model &&
                                !longLabel(k, w).toLowerCase().includes(w.model.toLowerCase()) &&
                                `${w.model} only`,
                            ]
                              .filter(Boolean)
                              .join(' · ')}
                      </span>
                    </li>
                  )
                })}
              </ul>
            ) : (
              <p className="usage-empty">{p.reason || 'No usage reported'}</p>
            )}
            {(p.stale || (p.reason && windows.length > 0)) && (
              <p className="usage-reported text-warning">
                {p.reason && `${p.reason}${p.stale ? ' · ' : ''}`}
                {p.stale && 'Stale data'}
              </p>
            )}
          </section>
        )
      })}
      <div className="usage-links">
        <Link to="/routing" onClick={onNavigate}>
          Routing &amp; limits <span aria-hidden="true">›</span>
        </Link>
        <Link to="/costs" onClick={onNavigate}>
          Costs &amp; usage history <span aria-hidden="true">›</span>
        </Link>
      </div>
    </>
  )
}

export function UsageStatus() {
  const [data, setData] = useState<Routing | null>(null)
  const [open, setOpen] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [loadFailed, setLoadFailed] = useState(false)
  const [, setTick] = useState(0)
  const id = useId()
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)

  const load = useCallback(() => {
    setRefreshing(true)
    return api
      .routing()
      .then(result => {
        setData(result)
        setLoadFailed(false)
      })
      .catch(() => setLoadFailed(true))
      .finally(() => setRefreshing(false))
  }, [])

  useEffect(() => {
    load()
    const poll = setInterval(load, POLL_MS)
    // Countdowns and pace markers move between polls.
    const tick = setInterval(() => setTick(t => t + 1), 20_000)
    return () => {
      clearInterval(poll)
      clearInterval(tick)
    }
  }, [load])

  const close = useCallback((restoreFocus = false) => {
    setOpen(false)
    if (restoreFocus) requestAnimationFrame(() => trigger.current?.focus())
  }, [])
  useDismissablePopover(open, close, trigger, panel)

  if (!data && !loadFailed) return null
  const providers = data ? usageProviders(data) : []
  const summary = providers
    .map(p => {
      const windows = Object.entries(p.windows)
      return `${p.name}: ${windows.length ? windows.map(([k, w]) => windowText(k, w)).join('; ') : 'no usage reported'}`
    })
    .join('. ')

  return (
    <div className="shell-popover usage-status">
      <button
        ref={trigger}
        type="button"
        className="usage-trigger"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={id}
        aria-label={`Usage. ${summary}`}
        onClick={() => {
          if (!open) load()
          setOpen(value => !value)
        }}
      >
        <UsageTrigger providers={providers} />
      </button>
      {open && (
        <div ref={panel} id={id} className="usage-popover" role="dialog" aria-label="Usage">
          {loadFailed && (
            <p className="usage-empty text-warning" role="status">
              Refresh failed{data ? ' · showing last data' : ''}
            </p>
          )}
          {data && <UsagePanel data={data} onRefresh={load} refreshing={refreshing} onNavigate={() => close()} />}
          {!data && (
            <button type="button" onClick={load}>
              Retry
            </button>
          )}
        </div>
      )}
    </div>
  )
}
