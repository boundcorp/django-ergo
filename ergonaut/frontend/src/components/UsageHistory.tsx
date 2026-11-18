import { useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { api } from '../api'
import type { SessionUsage, SessionUsageHistory, UsageTokenParts } from '../api'
import { hourLabel, UsageBars } from './UsageSparkline'

const parts: [keyof UsageTokenParts, string][] = [
  ['input_tokens', 'Input'],
  ['output_tokens', 'Output'],
  ['cache_write_tokens', 'Cache write'],
  ['cache_read_tokens', 'Cache read'],
]

type UsageRow = { label: string; thread: number; agents: number }

function UsageChart({ rows }: { rows: UsageRow[] }) {
  const [selected, setSelected] = useState<number | null>(null)
  const peak = Math.max(0, ...rows.map(row => row.thread + row.agents))
  const current = selected === null ? null : rows[selected]
  return (
    <div className="mt-4">
      <div className="flex justify-between text-xs text-muted">
        <span>{peak.toLocaleString()} tokens</span>
        <span>
          <span className="text-accent">●</span> Thread　<span className="text-mint">●</span> Known agents
        </span>
      </div>
      <div className="mt-2 flex h-36 items-end gap-1 border-b border-stroke sm:gap-3">
        {rows.map((row, index) => (
          <button
            key={`${row.label}-${index}`}
            type="button"
            aria-label={`${row.label}: ${row.thread.toLocaleString()} thread tokens, ${row.agents.toLocaleString()} known agent tokens`}
            onFocus={() => setSelected(index)}
            onClick={() => setSelected(index)}
            onMouseEnter={() => setSelected(index)}
            className="group flex h-full min-w-0 flex-1 flex-col justify-end rounded-control focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
          >
            <span
              className="mx-auto flex w-full max-w-10 flex-col-reverse overflow-hidden rounded-t"
              style={{ height: `${((row.thread + row.agents) / (peak || 1)) * 100}%` }}
            >
              <span
                className="bg-accent"
                style={{ height: `${(row.thread / (row.thread + row.agents || 1)) * 100}%` }}
              />
              <span className="bg-mint" style={{ height: `${(row.agents / (row.thread + row.agents || 1)) * 100}%` }} />
            </span>
          </button>
        ))}
      </div>
      <div className="mt-2 flex gap-1 text-center text-[10px] leading-tight text-muted sm:gap-3">
        {rows.map((row, index) => (
          <span key={`${row.label}-${index}`} className="min-w-0 flex-1 break-words">
            {row.label}
          </span>
        ))}
      </div>
      <p aria-live="polite" className="mt-3 min-h-5 text-xs text-muted">
        {current
          ? `${current.label}: ${current.thread.toLocaleString()} thread · ${current.agents.toLocaleString()} known agents`
          : 'Select a bar for exact values.'}
      </p>
    </div>
  )
}

function Composition({ rows }: { rows: UsageRow[] }) {
  const peak = Math.max(1, ...rows.map(row => row.thread + row.agents))
  return (
    <div className="mt-4 space-y-3">
      {rows.map((row, index) => (
        <div key={`${row.label}-${index}`}>
          <div className="flex flex-wrap justify-between gap-x-3 text-sm">
            <span className="min-w-0 break-all">{row.label}</span>
            <span className="text-muted tabular-nums">
              {row.thread.toLocaleString()} thread · {row.agents.toLocaleString()} agents
            </span>
          </div>
          <div
            className="mt-1 flex h-2 overflow-hidden rounded-full bg-raised"
            role="img"
            aria-label={`${row.label}: ${row.thread.toLocaleString()} thread tokens, ${row.agents.toLocaleString()} known agent tokens`}
          >
            <span className="bg-accent" style={{ width: `${(row.thread / peak) * 100}%` }} />
            <span className="bg-mint" style={{ width: `${(row.agents / peak) * 100}%` }} />
          </div>
        </div>
      ))}
    </div>
  )
}

/** The sparkline opens a snapshot of usage for this thread. */
export function UsageHistory({ sessionId, usage }: { sessionId: string; usage: SessionUsage }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const opener = useRef<HTMLButtonElement>(null)
  const titleId = useId()
  const [open, setOpen] = useState(false)
  const [history, setHistory] = useState<SessionUsageHistory | null>(null)
  const [error, setError] = useState(false)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    if (!open) return
    let live = true
    setHistory(null)
    setError(false)
    api
      .sessionUsageHistory(sessionId)
      .then(data => {
        if (live) setHistory(data)
      })
      .catch(() => {
        if (live) setError(true)
      })
    return () => {
      live = false
    }
  }, [open, sessionId, attempt])

  function close() {
    dialog.current?.close()
    setOpen(false)
    opener.current?.focus()
  }

  return (
    <>
      <button
        ref={opener}
        type="button"
        aria-haspopup="dialog"
        aria-label={`Open thread usage history: ${usage.thread_tokens.toLocaleString()} thread tokens and ${usage.agent_tokens.toLocaleString()} known agent tokens in the last ${usage.hours.length} hours; agent history incomplete`}
        onClick={() => {
          dialog.current?.showModal()
          setOpen(true)
        }}
        className="min-h-11 min-w-11 rounded-control text-left hover:bg-raised focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
      >
        <UsageBars usage={usage} />
      </button>
      {createPortal(
        <dialog
          ref={dialog}
          aria-labelledby={titleId}
          onCancel={event => {
            event.preventDefault()
            event.stopPropagation()
            close()
          }}
          onClose={event => {
            event.stopPropagation()
            setOpen(false)
            opener.current?.focus()
          }}
          className="m-auto max-h-[90dvh] w-[min(94vw,42rem)] overflow-y-auto rounded-card border border-stroke bg-surface p-0 text-ink shadow-xl backdrop:bg-black/40"
        >
          <div className="flex items-start gap-4 border-b border-stroke p-4">
            <div>
              <h2 id={titleId} className="font-display text-xl font-bold">
                Thread usage history
              </h2>
              <p className="mt-1 text-sm text-muted">Recorded thread usage and attributable coding-agent usage.</p>
            </div>
            <button
              type="button"
              autoFocus
              onClick={close}
              aria-label="Close usage history"
              className="ml-auto min-h-11 min-w-11 rounded-control text-muted hover:bg-raised"
            >
              ×
            </button>
          </div>
          <div className="space-y-5 p-4">
            <p className="rounded-control border border-stroke p-3 text-sm">
              Agent usage is incomplete or unknown. Orca worker usage is unavailable here because ownership cannot be
              proven; native-session events and legacy estimates are also excluded. Only explicitly owned local-shell
              run transcripts are included, and collection is periodic, so recent usage may be delayed. Zero recorded
              agent tokens does not mean no agent usage. Hourly and daily agent placement is estimated.
            </p>
            <section aria-label="Recent six hours">
              <h3 className="font-semibold">Recent {usage.hours.length} hours</h3>
              <p className="text-xs text-muted">
                Rolling hours, through {new Date(usage.until).toLocaleString()} (local time).
              </p>
              <div className="mt-4 grid grid-cols-2 gap-2">
                <div className="rounded-card border border-stroke bg-raised p-3">
                  <p className="text-xs text-muted">Thread tokens</p>
                  <p className="font-display text-xl font-bold tabular-nums">{usage.thread_tokens.toLocaleString()}</p>
                </div>
                <div className="rounded-card border border-stroke bg-raised p-3">
                  <p className="text-xs text-muted">Known agent tokens · partial</p>
                  <p className="font-display text-xl font-bold tabular-nums">{usage.agent_tokens.toLocaleString()}</p>
                </div>
              </div>
              {usage.hours.length ? (
                <UsageChart
                  rows={usage.hours.map((hour, index) => ({
                    label: hourLabel(index, usage.hours.length),
                    thread: hour.thread_tokens,
                    agents: hour.agent_tokens,
                  }))}
                />
              ) : (
                <p className="mt-2 text-sm text-muted">No recent hourly usage available.</p>
              )}
            </section>
            {error ? (
              <div role="alert" className="text-sm">
                <p>Could not load usage history.</p>
                <button
                  type="button"
                  onClick={() => setAttempt(n => n + 1)}
                  className="mt-2 rounded-control border border-stroke px-3 py-2 hover:bg-raised"
                >
                  Try again
                </button>
              </div>
            ) : !history ? (
              <p role="status" className="text-sm text-muted">
                Loading usage history…
              </p>
            ) : (
              <>
                <p className="text-xs text-muted">
                  History through {new Date(history.until).toLocaleString()} (local time) · Daily dates are UTC.
                </p>
                <section aria-label="Daily usage">
                  <h3 className="font-semibold">Last seven UTC days</h3>
                  {history.days.length ? (
                    <UsageChart
                      rows={history.days.map(day => ({
                        label: day.date,
                        thread: day.thread_tokens,
                        agents: day.agent_tokens,
                      }))}
                    />
                  ) : (
                    <p className="mt-2 text-sm text-muted">No daily usage available.</p>
                  )}
                </section>
                <section aria-label="All-time token breakdown">
                  <h3 className="font-semibold">All-time token breakdown</h3>
                  <p className="mt-1 text-sm text-muted">
                    Thread: {parts.reduce((sum, [key]) => sum + history.all_time.thread[key], 0).toLocaleString()} ·
                    Known agents: {parts.reduce((sum, [key]) => sum + history.all_time.agents[key], 0).toLocaleString()}
                  </p>
                  <Composition
                    rows={parts.map(([key, label]) => ({
                      label,
                      thread: history.all_time.thread[key],
                      agents: history.all_time.agents[key],
                    }))}
                  />
                </section>
                <section aria-label="Usage by model">
                  <h3 className="font-semibold">All-time usage by model</h3>
                  {history.models.length ? (
                    <Composition
                      rows={history.models.map(model => ({
                        label: model.model || '(unknown model)',
                        thread: model.thread_tokens,
                        agents: model.agent_tokens,
                      }))}
                    />
                  ) : (
                    <p className="mt-2 text-sm text-muted">No model usage available.</p>
                  )}
                </section>
              </>
            )}
            <p className="text-xs text-muted">
              Agent totals are estimated across each owned run’s active time; hourly and daily timing is approximate.
              Direct thread calls use recorded request times. Missing token values are not measured. Undated
              attributable usage is included only in all-time totals.
            </p>
          </div>
        </dialog>,
        document.body,
      )}
    </>
  )
}
