import { useId, type ReactNode } from 'react'
import type { DelegatedRequest, Worker } from '../api'
import { usePersistedFlag } from '../persistedFlag'

/** One shared activity disclosure for both desktop and mobile. */
export function ActivityStrip({
  requests,
  workers,
  children,
}: {
  requests: DelegatedRequest[]
  workers: Worker[]
  children: ReactNode
}) {
  const panelId = useId()
  const [open, setOpen] = usePersistedFlag('ergonaut.activity')
  const waiting = requests.filter(r => r.status !== 'answered' && r.status !== 'failed').length
  const active = workers.filter(w => w.status === 'queued' || w.status === 'running').length
  if (!waiting && !workers.length) return null
  const summary = [
    waiting ? `${waiting} request${waiting === 1 ? '' : 's'}` : '',
    workers.length ? `${active} active / ${workers.length} worker${workers.length === 1 ? '' : 's'}` : '',
  ]
    .filter(Boolean)
    .join(' · ')
  return (
    <section aria-label="Chat activity" className="mb-2 min-w-0">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        className="mx-4 flex max-w-[calc(100%-2rem)] items-center gap-2 rounded-control px-2 py-1 text-left text-xs text-muted hover:bg-raised sm:mx-6"
        onClick={() => {
          setOpen(!open)
        }}
      >
        <span aria-hidden="true">{open ? '▾' : '▸'}</span>
        <span className="shrink-0 font-medium text-ink">Activity</span>
        <span className="truncate">{summary}</span>
      </button>
      <div id={panelId} hidden={!open}>
        {open && children}
      </div>
    </section>
  )
}
