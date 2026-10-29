import { useEffect, useState } from 'react'
import type { SessionUsage, UsageHour } from '../api'
import { api } from '../api'
import { tokens } from '../pages/Costs'

// The chat header's token sparkline: one bar per hour for the last six, each split into the
// chat's own calls (bottom) and the coding agents its workers ran (top).

const HOURS = 6
const POLL_MS = 60_000
const BAR_HEIGHT = 24

// "now–1h", "3–2h ago": rolling hours, newest last.
export function hourLabel(index: number, count: number): string {
  const from = count - index
  return from === 1 ? 'Last hour' : `${from}–${from - 1}h ago`
}

export function barTitle(hour: UsageHour, index: number, count: number): string {
  const parts = [`${tokens(hour.thread_tokens)} thread`]
  if (hour.agent_tokens) parts.push(`${tokens(hour.agent_tokens)} agents`)
  return `${hourLabel(index, count)}: ${parts.join(' · ')}`
}

export function UsageBars({ usage }: { usage: SessionUsage }) {
  const peak = Math.max(1, ...usage.hours.map(h => h.thread_tokens + h.agent_tokens))
  const total = usage.thread_tokens + usage.agent_tokens
  const summary =
    `${tokens(total)} tokens in the last ${usage.hours.length}h` +
    (usage.agent_tokens ? ` (${tokens(usage.thread_tokens)} thread, ${tokens(usage.agent_tokens)} agents)` : '')
  return (
    <div className="usage-sparkline flex items-end gap-2" title={summary}>
      <div role="img" aria-label={summary} className="flex items-end gap-[3px]" style={{ height: BAR_HEIGHT }}>
        {usage.hours.map((hour, index) => {
          const height = (n: number) => (n ? Math.max(2, Math.round((n / peak) * BAR_HEIGHT)) : 0)
          const empty = !hour.thread_tokens && !hour.agent_tokens
          return (
            <div
              key={hour.start}
              data-hour={index}
              title={barTitle(hour, index, usage.hours.length)}
              className="flex w-2 flex-col-reverse overflow-hidden rounded-[2px]"
              style={{ height: BAR_HEIGHT }}
            >
              {empty ? (
                <div className="h-px bg-zinc-300 dark:bg-zinc-700" />
              ) : (
                <>
                  <div data-part="thread" className="bg-accent" style={{ height: height(hour.thread_tokens) }} />
                  <div data-part="agents" className="bg-mint" style={{ height: height(hour.agent_tokens) }} />
                </>
              )}
            </div>
          )
        })}
      </div>
      <div className="text-[11px] leading-tight text-muted tabular-nums">
        <div>
          <span className="font-semibold text-ink">{tokens(total)}</span> tokens · {usage.hours.length}h
        </div>
        <div className="flex items-center gap-2">
          <span className="flex items-center gap-1">
            <span className="inline-block size-1.5 rounded-full bg-accent" />
            {tokens(usage.thread_tokens)} thread
          </span>
          {usage.agent_tokens > 0 && (
            <span className="flex items-center gap-1">
              <span className="inline-block size-1.5 rounded-full bg-mint" />
              {tokens(usage.agent_tokens)} agents
            </span>
          )}
        </div>
      </div>
    </div>
  )
}

/** The chat's hourly usage, fetched when it opens, when `refreshKey` changes (a new message), and each minute. */
export function useSessionUsage(sessionId: string, refreshKey?: string | number): SessionUsage | null {
  const [usage, setUsage] = useState<SessionUsage | null>(null)
  useEffect(() => setUsage(null), [sessionId])
  useEffect(() => {
    let live = true
    const load = () =>
      api
        .sessionUsage(sessionId, HOURS)
        .then(u => live && setUsage(u))
        .catch(() => {})
    load()
    const timer = setInterval(load, POLL_MS)
    return () => {
      live = false
      clearInterval(timer)
    }
  }, [sessionId, refreshKey])
  return usage
}
