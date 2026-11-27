import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import type { DelegatedRequest, Worker } from '../api'
import { usePersistedFlag } from '../persistedFlag'
import {
  WORKER_STATE,
  agentDetail,
  chipModel,
  chipTitles,
  elapsed,
  isLive,
  span,
  stripOrder,
  workerState,
  workerTitle,
  workersCount,
  type WorkerState,
} from '../workerDisplay'
import { WorkerTerminal, headline, useNow } from './WorkerActivity'

/**
 * A chat's activity on one line: a badge per worker (state, short title, model) side by side,
 * scrolling sideways when they don't fit, with a "+N" list of all of them. A badge opens the
 * worker's terminal. Open delegated requests sit behind their own toggle below the line.
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
  const [terminal, setTerminal] = useState<string | null>(null)
  const pending = requests.filter(r => r.status !== 'answered' && r.status !== 'failed')
  const approvals = pending.some(r => r.direction === 'out' && r.approvals?.length)
  if (!pending.length && !workers.length) return null
  const ordered = stripOrder(workers, now)
  const shown = workers.find(w => w.id === terminal)
  return (
    <section aria-label="Chat activity" className="relative mx-4 mb-2 min-w-0 sm:mx-6">
      <div className="flex h-7 min-w-0 items-center gap-1.5 text-xs" data-testid="activity-line">
        {!!workers.length && <span className="shrink-0 font-medium text-muted">{workersCount(workers)}</span>}
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
        {!!workers.length && <WorkerChips workers={ordered} now={now} onOpen={setTerminal} />}
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
      {shown && <WorkerTerminal worker={shown} onClose={() => setTerminal(null)} />}
    </section>
  )
}

const DOT: Record<WorkerState, string> = {
  queued: 'bg-muted/60',
  running: 'bg-accent motion-safe:animate-pulse',
  waiting: 'bg-warning',
  stalled: 'border border-warning bg-transparent',
  done: 'bg-success',
  needs_run: 'border border-warning bg-transparent',
  failed: 'bg-danger',
  cancelled: 'border border-muted bg-transparent',
}

const CHIP: Record<WorkerState, string> = {
  queued: 'border-stroke bg-surface',
  running: 'border-stroke bg-surface',
  waiting: 'border-warning bg-amber-tint text-warning font-semibold shadow-[0_0_0_1px_var(--color-warning)]',
  stalled: 'border-warning/50 bg-surface',
  done: 'border-stroke/60 bg-surface opacity-75',
  needs_run: 'border-warning/50 bg-surface',
  failed: 'border-danger/60 bg-red-tint',
  cancelled: 'border-stroke/60 bg-surface opacity-60',
}

/** A state dot; a waiting one rings so it catches the eye. */
function StateDot({ state }: { state: WorkerState }) {
  return (
    <span className="relative flex h-2 w-2 shrink-0" aria-hidden="true" data-testid="state-dot">
      {state === 'waiting' && (
        <span className="absolute inline-flex h-full w-full rounded-full bg-warning opacity-75 motion-safe:animate-ping" />
      )}
      <span className={`relative inline-flex h-2 w-2 rounded-full ${DOT[state]}`} />
    </span>
  )
}

/** Everything a chip leaves out, for its tooltip. */
export function chipTooltip(worker: Worker, now: number): string {
  const state = workerState(worker, now)
  const ran = elapsed(worker, now)
  const at = worker.activity?.at
  const line = headline(worker)
  return [
    worker.title,
    WORKER_STATE[state].label + (ran != null ? ` · ${isLive(worker) ? 'running for' : 'ran for'} ${span(ran)}` : ''),
    worker.agent ? agentDetail(worker.agent) : '',
    isLive(worker) && at ? `Last output ${span(now - at)} ago` : '',
    line ? `Latest: ${line}` : '',
    'Click to open its terminal',
  ]
    .filter(Boolean)
    .join('\n')
}

function WorkerChip({
  worker,
  label,
  now,
  onOpen,
}: {
  worker: Worker
  label: string
  now: number
  onOpen: (id: string) => void
}) {
  const state = workerState(worker, now)
  const model = chipModel(worker)
  const title = workerTitle(worker)
  return (
    <button
      type="button"
      role="listitem"
      data-testid="worker-chip"
      data-state={state}
      title={chipTooltip(worker, now)}
      aria-label={`${title}, ${WORKER_STATE[state].label}${model ? `, ${model}` : ''}. Open its terminal`}
      onClick={() => onOpen(worker.id)}
      className={`inline-flex h-6 shrink-0 items-center gap-1.5 rounded-full border px-2 text-ink hover:bg-raised ${CHIP[state]}`}
    >
      <StateDot state={state} />
      <span className="max-w-[8.5rem] truncate sm:max-w-[13rem]">{label}</span>
      {model && (
        <span
          className={`shrink-0 font-mono text-[11px] font-normal ${state === 'waiting' ? 'text-warning' : 'text-muted'}`}
          data-testid="chip-model"
        >
          {model}
        </span>
      )}
    </button>
  )
}

/** How many of a scroller's children reach past its right edge (partly or wholly hidden). */
export function hiddenRight(scroller: HTMLElement): number {
  const edge = scroller.getBoundingClientRect().right + 1
  return Array.from(scroller.children).filter(child => child.getBoundingClientRect().right > edge).length
}

/** The badges, scrolling sideways, and a "+N" for the ones past the edge. */
function WorkerChips({ workers, now, onOpen }: { workers: Worker[]; now: number; onOpen: (id: string) => void }) {
  const scroller = useRef<HTMLDivElement>(null)
  const labels = chipTitles(workers)
  const [hidden, setHidden] = useState(0)
  const measure = useCallback(() => {
    if (scroller.current) setHidden(hiddenRight(scroller.current))
  }, [])
  useLayoutEffect(measure, [measure, workers])
  useEffect(() => {
    const el = scroller.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(el)
    return () => observer.disconnect()
  }, [measure])
  return (
    <>
      <div
        ref={scroller}
        role="list"
        aria-label="Workers"
        onScroll={measure}
        className={`activity-chips flex min-w-0 flex-1 items-center gap-1.5 overflow-x-auto overflow-y-hidden ${hidden ? 'activity-chips-fade' : ''}`}
        data-testid="worker-chips"
      >
        {workers.map(w => (
          <WorkerChip key={w.id} worker={w} label={labels.get(w.id) ?? ''} now={now} onOpen={onOpen} />
        ))}
      </div>
      {hidden > 0 && <OverflowList workers={workers} hidden={hidden} now={now} onOpen={onOpen} />}
    </>
  )
}

/** "+N": every worker in a list, with the detail a badge has no room for. */
function OverflowList({
  workers,
  hidden,
  now,
  onOpen,
}: {
  workers: Worker[]
  hidden: number
  now: number
  onOpen: (id: string) => void
}) {
  const [open, setOpen] = useState(false)
  const box = useRef<HTMLDivElement>(null)
  const listId = useId()
  useEffect(() => {
    if (!open) return
    const away = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false)
    }
    const escape = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false)
    document.addEventListener('mousedown', away)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('mousedown', away)
      document.removeEventListener('keydown', escape)
    }
  }, [open])
  const waitingHidden = workers.some(w => workerState(w, now) === 'waiting')
  return (
    <div ref={box} className="shrink-0">
      <button
        type="button"
        aria-haspopup="true"
        aria-expanded={open}
        aria-controls={listId}
        title="All workers"
        data-testid="chips-overflow"
        onClick={() => setOpen(!open)}
        className={`inline-flex h-6 items-center rounded-full border px-2 font-medium hover:bg-raised ${
          open ? 'border-accent bg-raised text-ink' : 'border-stroke bg-surface text-muted'
        }`}
      >
        +{hidden}
      </button>
      {open && (
        <div
          id={listId}
          role="menu"
          aria-label="All workers"
          data-testid="chips-popover"
          className="absolute right-0 top-8 z-30 max-h-[min(24rem,60vh)] w-[min(26rem,100%)] overflow-y-auto rounded-card border border-stroke bg-surface p-1 shadow-xl"
        >
          {workers.map(w => {
            const state = workerState(w, now)
            const model = chipModel(w)
            const ran = elapsed(w, now)
            return (
              <button
                key={w.id}
                type="button"
                role="menuitem"
                title={chipTooltip(w, now)}
                onClick={() => {
                  setOpen(false)
                  onOpen(w.id)
                }}
                className={`flex w-full min-w-0 items-center gap-2 rounded-control px-2 py-1.5 text-left text-xs hover:bg-raised ${
                  state === 'waiting' ? 'bg-amber-tint' : ''
                }`}
              >
                <StateDot state={state} />
                <span className="min-w-0 flex-1 truncate text-ink">{workerTitle(w)}</span>
                {model && <span className="shrink-0 font-mono text-[11px] text-muted">{model}</span>}
                <span
                  className={`shrink-0 text-[11px] ${state === 'waiting' ? 'font-semibold text-warning' : 'text-muted'}`}
                >
                  {WORKER_STATE[state].label}
                  {ran != null && state !== 'waiting' ? ` · ${span(ran)}` : ''}
                </span>
              </button>
            )
          })}
          {waitingHidden && <span className="sr-only">One or more workers are waiting on you.</span>}
        </div>
      )}
    </div>
  )
}
