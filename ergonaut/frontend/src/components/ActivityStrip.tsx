import { useId, type ReactNode } from 'react'
import type { DelegatedRequest, Worker } from '../api'
import { usePersistedFlag } from '../persistedFlag'
import { stripOrder, workersCount } from '../workerDisplay'
import { useNow } from './WorkerActivity'
import { WorkerBadges } from './WorkerBadges'

export { chipTooltip, hiddenRight } from './WorkerBadges'

/**
 * The chat header's activity line, the one place a chat's workers show: a badge per worker
 * (state, short title, model) side by side on one line, waiting-on-you first, scrolling sideways
 * with a "+N" list when they don't fit. A badge opens the worker's terminal. Open delegated
 * requests sit behind their own toggle on the same line.
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
  /** The open requests, shown when their toggle is open. */
  children?: ReactNode
}) {
  const panelId = useId()
  const now = useNow()
  const [open, setOpen] = usePersistedFlag(sessionId ? `ergonaut.activity.${sessionId}` : 'ergonaut.activity')
  const pending = requests.filter(r => r.status !== 'answered' && r.status !== 'failed')
  const approvals = pending.some(r => r.direction === 'out' && r.approvals?.length)
  if (!pending.length && !workers.length) return null
  const ordered = stripOrder(workers, now)
  return (
    <section aria-label="Chat activity" className="relative mx-4 mb-2 min-w-0 sm:mx-6">
      <div className="flex h-7 min-w-0 items-center gap-1.5 text-xs" data-testid="activity-line">
        {!!workers.length && (
          <span className="hidden shrink-0 font-medium text-muted sm:inline">{workersCount(workers)}</span>
        )}
        {!!pending.length && (
          <button
            type="button"
            aria-expanded={open}
            aria-controls={panelId}
            className={`inline-flex h-6 shrink-0 items-center gap-1 rounded-full border px-2 hover:bg-raised ${
              approvals
                ? 'border-warning bg-amber-tint font-semibold text-warning'
                : 'border-stroke bg-surface text-muted'
            }`}
            onClick={() => setOpen(!open)}
          >
            <span aria-hidden="true">{open ? '▾' : '▸'}</span>
            {pending.length} request{pending.length === 1 ? '' : 's'}
          </button>
        )}
        {!!workers.length && <WorkerBadges workers={ordered} now={now} />}
      </div>
      {!!pending.length && (
        <div
          id={panelId}
          hidden={!open}
          className="mt-1 max-h-[min(14rem,32vh)] overflow-y-auto overscroll-contain"
          data-testid="activity-panel"
        >
          {open && children}
        </div>
      )}
    </section>
  )
}
