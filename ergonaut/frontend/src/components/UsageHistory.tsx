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

function BreakdownTable({ rows }: { rows: { label: string; thread: number; agents: number }[] }) {
  return (
    <table className="w-full text-left text-sm tabular-nums">
      <thead className="text-xs text-muted">
        <tr>
          <th scope="col" className="py-2 font-medium">
            Period / model
          </th>
          <th scope="col" className="py-2 text-right font-medium">
            Thread
          </th>
          <th scope="col" className="py-2 text-right font-medium">
            Agents (partial)
          </th>
        </tr>
      </thead>
      <tbody>
        {rows.map(row => (
          <tr key={row.label} className="border-t border-stroke">
            <th scope="row" className="break-all py-2 pr-2 font-normal">
              {row.label}
            </th>
            <td className="py-2 text-right">{row.thread.toLocaleString()}</td>
            <td className="py-2 text-right">{row.agents.toLocaleString()}</td>
          </tr>
        ))}
      </tbody>
    </table>
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
              Agent history is incomplete. Only explicitly owned local-shell run transcripts are included.
              Native-session events and legacy estimates are excluded because thread ownership cannot be proven. Zero
              does not mean no agent usage.
            </p>
            <section aria-label="Recent six hours">
              <h3 className="font-semibold">Recent {usage.hours.length} hours</h3>
              <p className="text-xs text-muted">
                Rolling hours, through {new Date(usage.until).toLocaleString()} (local time).
              </p>
              {usage.hours.length ? (
                <BreakdownTable
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
                    <BreakdownTable
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
                  <BreakdownTable
                    rows={[
                      {
                        label: 'Total',
                        thread: parts.reduce((sum, [key]) => sum + history.all_time.thread[key], 0),
                        agents: parts.reduce((sum, [key]) => sum + history.all_time.agents[key], 0),
                      },
                      ...parts.map(([key, label]) => ({
                        label,
                        thread: history.all_time.thread[key],
                        agents: history.all_time.agents[key],
                      })),
                    ]}
                  />
                </section>
                <section aria-label="Usage by model">
                  <h3 className="font-semibold">All-time usage by model</h3>
                  {history.models.length ? (
                    <BreakdownTable
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
