import { useId, useState, type ReactNode } from 'react'
import type { DelegatedRequest, Todo, Worker } from '../api'
import { todoProgress } from './TodoList'

const STORAGE_KEY = 'ergonaut.activity'

export function activityExpanded(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) === 'open'
  } catch {
    return false
  }
}

export function saveActivityExpanded(open: boolean) {
  try {
    localStorage.setItem(STORAGE_KEY, open ? 'open' : 'closed')
  } catch {
    // Storage may be unavailable in private browsing.
  }
}

/** One shared activity disclosure for both desktop and mobile. */
export function ActivityStrip({
  requests,
  workers,
  todos,
  children,
}: {
  requests: DelegatedRequest[]
  workers: Worker[]
  todos: Todo[]
  children: ReactNode
}) {
  const panelId = useId()
  const [open, setOpen] = useState(activityExpanded)
  const waiting = requests.filter(r => r.status !== 'answered' && r.status !== 'failed').length
  const active = workers.filter(w => w.status === 'queued' || w.status === 'running').length
  if (!waiting && !workers.length && !todos.length) return null
  const current = todos.find(t => t.status === 'in_progress')
  const summary = [
    waiting ? `${waiting} request${waiting === 1 ? '' : 's'}` : '',
    workers.length ? `${active} active / ${workers.length} worker${workers.length === 1 ? '' : 's'}` : '',
    todos.length ? todoProgress(todos) : '',
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
          saveActivityExpanded(!open)
        }}
      >
        <span aria-hidden="true">{open ? '▾' : '▸'}</span>
        <span className="shrink-0 font-medium text-ink">Activity</span>
        <span className="truncate">
          {summary}
          {current ? ` · ${current.content}` : ''}
        </span>
      </button>
      <div id={panelId} hidden={!open}>
        {open && children}
      </div>
    </section>
  )
}
