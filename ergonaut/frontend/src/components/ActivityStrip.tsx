import { useId, type ReactNode } from 'react'
import type { DelegatedRequest, Worker } from '../api'
import { usePersistedFlag } from '../persistedFlag'
import { WORKER_STATE, workerState, workersSummary } from '../workerDisplay'
import { useNow } from './WorkerActivity'

/**
 * A chat's activity (requests and workers) behind one summary line, open or closed per chat.
 * Open, it stays short: the list scrolls inside it rather than pushing the chat down.
 */
export function ActivityStrip({
  sessionId = '',
  requests,
  workers,
  children,
}: {
  sessionId?: string
  requests: DelegatedRequest[]
  workers: Worker[]
  children: ReactNode
}) {
  const panelId = useId()
  const now = useNow()
  const [open, setOpen] = usePersistedFlag(sessionId ? `ergonaut.activity.${sessionId}` : 'ergonaut.activity')
  const waiting = requests.filter(r => r.status !== 'answered' && r.status !== 'failed').length
  if (!waiting && !workers.length) return null
  const summary = [waiting ? `${waiting} request${waiting === 1 ? '' : 's'}` : '', workersSummary(workers, now)]
    .filter(Boolean)
    .join(' · ')
  return (
    <section aria-label="Chat activity" className="mx-4 mb-2 min-w-0 sm:mx-6">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        className="flex max-w-full items-center gap-2 rounded-full border border-stroke bg-surface px-2.5 py-0.5 text-left text-xs text-muted hover:bg-raised"
        onClick={() => {
          setOpen(!open)
        }}
      >
        <span aria-hidden="true">{open ? '▾' : '▸'}</span>
        <span className="shrink-0 font-medium text-ink">Activity</span>
        {!!workers.length && (
          <span className="flex shrink-0 items-center gap-0.5" aria-hidden="true">
            {workers.slice(0, 8).map(w => {
              const state = workerState(w, now)
              return (
                <span
                  key={w.id}
                  className={`h-2 w-2 rounded-full border border-current ${WORKER_STATE[state].badge} ${state === 'running' ? 'motion-safe:animate-pulse' : ''}`}
                  title={`${w.title}: ${WORKER_STATE[state].label}`}
                />
              )
            })}
          </span>
        )}
        <span className="truncate">{summary}</span>
      </button>
      <div
        id={panelId}
        hidden={!open}
        className="mt-1 max-h-[min(14rem,32vh)] overflow-y-auto overscroll-contain"
        data-testid="activity-panel"
      >
        {open && children}
      </div>
    </section>
  )
}
