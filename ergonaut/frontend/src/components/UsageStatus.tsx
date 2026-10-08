import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import type { Routing, RoutingProvider, RoutingWindow } from '../api'
import { api } from '../api'
import { agoLong } from '../time'
import { useDismissablePopover } from './ShellControls'

// The top bar's subscription usage: one ring per reported limit window, each with
// a pace marker (how much of the window has passed), and a popover with the detail.
// Data comes from GET /api/routing, which already carries each provider's windows.

const HOUR = 3600
const DAY = 24 * HOUR
const POLL_MS = 60_000

// How long a window lasts, for the pace marker; null when the window ID doesn't say.
export function windowSeconds(key: string): number | null {
  if (key === 'five_hour') return 5 * HOUR
  if (key === 'weekly' || key.startsWith('weekly_')) return 7 * DAY
  const minutes = /^minutes_(\d+)$/.exec(key)
  return minutes ? Number(minutes[1]) * 60 : null
}

// Percent of the window already gone (0-100), or null without a reset time or known length.
export function pace(key: string, w: RoutingWindow, now = Date.now() / 1000): number | null {
  const length = windowSeconds(key)
  if (!length || !w.resets_at) return null
  const left = w.resets_at - now
  if (left <= 0) return null
  return Math.max(0, Math.min(100, ((length - left) / length) * 100))
}

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

// "4:20 PM" within a day, else "Mon 8:00 AM".
function resetsAt(at: number, now = Date.now() / 1000): string {
  const when = new Date(at * 1000)
  const time = when.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
  return at - now < DAY ? time : `${when.toLocaleDateString([], { weekday: 'short' })} ${time}`
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

// The window closest to its routing limit, across every subscription.
export function tightest(providers: RoutingProvider[]): Entry | null {
  let best: Entry | null = null
  for (const provider of providers.filter(p => p.subscription))
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
    <div className={`usage-bar usage-tone-${tone(w)}`} aria-hidden="true">
      {w.used != null && <div className="usage-bar-fill" style={{ width: `${Math.max(0, Math.min(100, used))}%` }} />}
      {passed != null && <div className="usage-bar-pace" style={{ left: `${passed}%` }} />}
    </div>
  )
}

function paceNote(k: string, w: RoutingWindow, now?: number): string {
  const passed = pace(k, w, now)
  if (passed == null || w.used == null) return ''
  if (w.used > passed + 10) return 'ahead of pace'
  if (w.used < passed - 10) return 'under pace'
  return 'on pace'
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
  const subs = providers.filter(p => p.subscription)
  return (
    <>
      {subs.map((p, i) => {
        const windows = Object.entries(p.windows)
        // On the narrowest screens only each provider's tightest window keeps its ring.
        const tight = tightest([p])
        return (
          <span key={p.name} className="usage-provider">
            {i > 0 && <span className="usage-divider" aria-hidden="true" />}
            <Glyph type={p.type} />
            {windows.length ? (
              windows.map(([k, w]) => {
                const primary = tight ? tight.key === k : k === windows[0][0]
                return (
                  <span
                    key={k}
                    className={`usage-limit${primary ? ' usage-primary' : ''}`}
                    title={windowText(k, w, now)}
                  >
                    <Ring k={k} w={w} now={now} />
                    <span className="usage-limit-text">
                      <span className="usage-limit-value">{w.used == null ? '–' : `${Math.round(w.used)}%`}</span>
                      <span className="usage-limit-tag">{shortTag(k, w)}</span>
                    </span>
                  </span>
                )
              })
            ) : (
              <span className="usage-limit usage-primary" title="No usage reported yet">
                <Ring k="" w={{ used: null, resets_at: null, limit: 100 }} />
              </span>
            )}
          </span>
        )
      })}
    </>
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
  const subs = data.providers.filter(p => p.subscription)
  const apiKeys = data.providers.filter(p => !p.subscription)
  const tight = tightest(subs)
  return (
    <>
      <div className="usage-head">
        <span className="font-semibold text-ink">Usage</span>
        <span className="text-xs text-muted">
          {subs.length === 1 ? '1 subscription' : `${subs.length} subscriptions`}
        </span>
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
      {tight && (
        <div className={`usage-headline usage-tone-${tone(tight.window)}`}>
          <div className="text-sm text-ink">
            <strong>
              {Math.round(tight.window.used!)}% of {tight.provider.name} {longLabel(tight.key, tight.window)} used.
            </strong>
            {tight.window.resets_at && <> Resets {resetsAt(tight.window.resets_at, now)}.</>}
          </div>
          <div className="text-xs text-muted">
            {tone(tight.window) === 'over'
              ? `Past its ${tight.window.limit}% routing limit, so auto tiers skip it until it resets.`
              : 'That is your tightest limit right now.'}
          </div>
        </div>
      )}
      {!subs.length && (
        <p className="usage-empty">
          {data.providers.length ? 'No subscription providers configured.' : 'No providers.yaml is loaded.'}
        </p>
      )}
      {subs.map(p => {
        const windows = Object.entries(p.windows)
        const soonest = windows
          .map(([, w]) => w.resets_at)
          .filter((at): at is number => !!at && at > (now ?? Date.now() / 1000))
          .sort((a, b) => a - b)[0]
        return (
          <section key={p.name} className="usage-section" aria-label={`${p.name} usage`}>
            <div className="usage-section-head">
              <span className="usage-glyph-box">
                <Glyph type={p.type} />
              </span>
              <span className="font-semibold text-ink">{p.name}</span>
              {soonest && <span className="text-xs text-muted">Resets in {countdown(soonest, now)}</span>}
              {p.status === 'in_use' && <span className="usage-chip">In use</span>}
              {p.status === 'skipped' && <span className="usage-chip usage-chip-danger">Skipped</span>}
            </div>
            {windows.length ? (
              <ul className="usage-rows">
                {windows.map(([k, w]) => {
                  const note = paceNote(k, w, now)
                  return (
                    <li key={k} className="usage-row" title={windowText(k, w, now)}>
                      <span className="usage-row-label">{longLabel(k, w)}</span>
                      <Bar k={k} w={w} now={now} />
                      <span className="usage-row-value">{w.used == null ? '–' : `${Math.round(w.used)}%`}</span>
                      <span className="usage-row-meta">
                        {w.used == null
                          ? w.status === 'reset'
                            ? 'Reset · not reported since'
                            : 'Not reported'
                          : [w.resets_at && countdown(w.resets_at, now), note, w.model && `${w.model} only`]
                              .filter(Boolean)
                              .join(' · ')}
                      </span>
                    </li>
                  )
                })}
              </ul>
            ) : (
              <p className="usage-empty">No usage reported yet. It shows up after the first call.</p>
            )}
            {p.reported_at && (
              <p className={`usage-reported${p.stale ? ' text-warning' : ''}`}>
                {p.stale ? 'Stale snapshot · ' : ''}Reported {agoLong(p.reported_at, (now ?? Date.now() / 1000) * 1000)}
                {p.reason && ` · ${p.reason}`}
              </p>
            )}
          </section>
        )
      })}
      {apiKeys.length > 0 && (
        <p className="usage-note">Pay per token, no limits reported: {apiKeys.map(p => p.name).join(', ')}.</p>
      )}
      {subs.length > 0 && (
        <p className="usage-note usage-legend">
          <span className="usage-legend-tick" aria-hidden="true" /> marks how much of each window has passed. A fill
          past it is burning faster than the window allows.
        </p>
      )}
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
  const [, setTick] = useState(0)
  const id = useId()
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)

  const load = useCallback(() => {
    setRefreshing(true)
    return api
      .routing()
      .then(setData)
      .catch(() => {})
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

  if (!data || !data.providers.some(p => p.subscription)) return null
  const summary = data.providers
    .filter(p => p.subscription)
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
        <UsageTrigger providers={data.providers} />
      </button>
      {open && (
        <div ref={panel} id={id} className="usage-popover" role="dialog" aria-label="Usage">
          <UsagePanel data={data} onRefresh={load} refreshing={refreshing} onNavigate={() => close()} />
        </div>
      )}
    </div>
  )
}
