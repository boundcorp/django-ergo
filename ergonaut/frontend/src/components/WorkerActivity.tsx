import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { Worker, WorkerEntry, WorkerLog } from '../api'
import { api } from '../api'
import {
  WORKER_STATE,
  agentDetail,
  agentLabel,
  elapsed,
  isLive,
  latestLine,
  span,
  stripAnsi,
  workerState,
  workerTitle,
} from '../workerDisplay'

// A chat's workers, compact: one row each (state, agent and model, title, latest output and
// times) and their full output in a terminal view opened on demand.

const LOG_REFRESH_MS = 5_000

/** The current time in epoch seconds, ticking every `ms`. */
export function useNow(ms = 15_000): number {
  const [now, setNow] = useState(() => Date.now() / 1000)
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), ms)
    return () => clearInterval(timer)
  }, [ms])
  return now
}

/** The worker's state as a coloured badge. */
export function WorkerBadge({ worker, now }: { worker: Worker; now: number }) {
  const state = workerState(worker, now)
  const { label, badge } = WORKER_STATE[state]
  const waiting = state === 'waiting' ? worker.activity?.waiting : ''
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1 rounded-full px-1.5 py-px text-[11px] font-medium ${badge}`}
      title={waiting ? `Waiting on you: ${waiting}` : undefined}
    >
      {state === 'running' && (
        <span
          aria-hidden="true"
          className="h-1.5 w-1.5 rounded-full bg-current motion-safe:animate-pulse"
          data-testid="running-dot"
        />
      )}
      {label}
    </span>
  )
}

/** "claude · Haiku 5.5 · low", or nothing for a worker that isn't a coding agent. */
export function WorkerModel({ worker, withAgent = true }: { worker: Worker; withAgent?: boolean }) {
  if (!worker.agent) return null
  return (
    <span
      className="shrink-0 rounded bg-raised px-1.5 py-px font-mono text-[11px] text-muted"
      title={agentDetail(worker.agent)}
      data-testid="worker-model"
    >
      {agentLabel(worker.agent, withAgent)}
    </span>
  )
}

/** How long it has run, and how long since its last output. */
function WorkerTimes({ worker, now }: { worker: Worker; now: number }) {
  const ran = elapsed(worker, now)
  const at = worker.activity?.at
  return (
    <span className="shrink-0 whitespace-nowrap text-[11px] tabular-nums text-muted">
      {ran != null && <span title={isLive(worker) ? 'Running for' : 'Ran for'}>{span(ran)}</span>}
      {isLive(worker) && at && (
        <span title={worker.activity?.checked_at ? `Checked ${span(now - worker.activity.checked_at)} ago` : undefined}>
          {ran != null ? ' · ' : ''}out {span(now - at)} ago
        </span>
      )}
    </span>
  )
}

/** What to show as a worker's one line of news: its question, error, output or progress. */
export function headline(worker: Worker): string {
  if (worker.status === 'running' && worker.activity?.waiting) return `asks: ${worker.activity.waiting}`
  if (worker.status === 'failed' && worker.error) return latestLine([{ kind: 'error', text: worker.error, at: null }])
  return latestLine(worker.activity?.entries) || latestLine([{ kind: 'progress', text: worker.progress, at: null }])
}

/** Whether a worker's output can be read (now or as last kept). */
export function hasLog(worker: Worker): boolean {
  const kind = (worker.function ?? '').split(':')[0]
  return (!!worker.session_id && (kind === 'orca' || kind === 'agent')) || !!worker.activity?.entries?.length
}

/**
 * One worker in one row (two on a phone): state, title, agent and model, its latest line of
 * output and its times, with a button that opens its terminal.
 */
export function WorkerRow({ worker, showLatest = true }: { worker: Worker; showLatest?: boolean }) {
  const now = useNow()
  const [open, setOpen] = useState(false)
  const line = showLatest ? headline(worker) : ''
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 py-1 text-xs" data-testid="worker-row">
      <span className="order-1">
        <WorkerBadge worker={worker} now={now} />
      </span>
      <span
        className="order-2 min-w-0 flex-1 truncate font-medium text-ink sm:max-w-[34%] sm:flex-none"
        title={worker.title}
      >
        {workerTitle(worker)}
      </span>
      <span className="order-5 sm:order-3">
        <WorkerModel worker={worker} />
      </span>
      {line ? (
        <span className="order-7 min-w-0 flex-1 truncate font-mono text-[11px] text-muted sm:order-4" title={line}>
          {line}
        </span>
      ) : (
        <span className="order-7 flex-1 sm:order-4" />
      )}
      <span className="order-6 sm:order-5">
        <WorkerTimes worker={worker} now={now} />
      </span>
      {hasLog(worker) && (
        <button
          type="button"
          className="order-3 shrink-0 rounded-control border border-stroke px-1.5 py-px font-mono text-[11px] text-muted hover:bg-raised hover:text-ink sm:order-6"
          title="Open its terminal output"
          aria-label={`Open output of ${workerTitle(worker)}`}
          onClick={e => {
            e.stopPropagation()
            setOpen(true)
          }}
        >
          &gt;_ log
        </button>
      )}
      {/* On a phone the row breaks here: model, times and output on the second line. */}
      <span className="order-4 h-0 basis-full sm:hidden" aria-hidden="true" />
      {open && <WorkerTerminal worker={worker} onClose={() => setOpen(false)} />}
    </div>
  )
}

const MARK: Record<string, { mark: string; tone: string }> = {
  tool: { mark: '▸', tone: 'text-accent' },
  result: { mark: '↳', tone: 'text-muted' },
  error: { mark: '✗', tone: 'text-danger' },
  reasoning: { mark: '…', tone: 'text-muted italic' },
  user: { mark: '›', tone: 'text-muted' },
  assistant: { mark: '●', tone: 'text-ink' },
  terminal: { mark: '', tone: 'text-ink' },
}

function TerminalEntry({ entry }: { entry: WorkerEntry }) {
  const kind = MARK[entry.kind] ?? { mark: '•', tone: 'text-ink' }
  return (
    <div className={`flex gap-2 ${kind.tone}`}>
      {kind.mark && <span className="w-3 shrink-0 select-none text-center">{kind.mark}</span>}
      <span className="whitespace-pre">{stripAnsi(entry.text)}</span>
    </div>
  )
}

/** Whether a scroller sits at (or within a line of) its bottom. */
function atBottom(el: HTMLElement): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 24
}

/**
 * A worker's recent output in a terminal: monospace, line breaks kept, long lines scrolling
 * sideways. It refreshes while the worker runs and follows the end unless you scrolled up.
 */
export function WorkerTerminal({ worker, onClose }: { worker: Worker; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const scroller = useRef<HTMLDivElement>(null)
  const follow = useRef(true)
  const [following, setFollowing] = useState(true)
  const [log, setLog] = useState<WorkerLog | null>(null)
  const [error, setError] = useState('')
  const now = useNow(5_000)
  const live = isLive(worker)
  const sessionId = worker.session_id
  // Activity the chat already has, until (or instead of) a fresh read.
  const entries = log?.entries ?? worker.activity?.entries ?? []

  useEffect(() => {
    const el = dialog.current
    if (el && !el.open) {
      try {
        el.showModal()
      } catch {
        el.setAttribute('open', '')
      }
    }
  }, [])

  useEffect(() => {
    if (!sessionId) return
    let alive = true
    const load = () =>
      api
        .workerLog(sessionId, worker.id)
        .then(found => {
          if (!alive) return
          setLog(found)
          setError('')
        })
        .catch(e => alive && setError(String(e?.message ?? e)))
    load()
    const timer = live ? setInterval(load, LOG_REFRESH_MS) : undefined
    return () => {
      alive = false
      if (timer) clearInterval(timer)
    }
  }, [sessionId, worker.id, live])

  useLayoutEffect(() => {
    const el = scroller.current
    if (el && follow.current) el.scrollTop = el.scrollHeight
  }, [entries])

  function jumpToEnd() {
    const el = scroller.current
    if (!el) return
    el.scrollTop = el.scrollHeight
    follow.current = true
    setFollowing(true)
  }

  const source = log
    ? `${log.source || 'output'}${log.live ? '' : ' · as of the last check'}`
    : sessionId
      ? 'Reading…'
      : 'No terminal kept for this worker'
  return (
    <dialog
      ref={dialog}
      aria-label={`Output of ${workerTitle(worker)}`}
      onClose={onClose}
      onClick={e => {
        if (e.target === dialog.current) dialog.current?.close()
      }}
      className="m-auto w-[min(96vw,72rem)] max-w-none rounded-card border border-stroke bg-surface p-0 text-ink shadow-xl backdrop:bg-black/50"
    >
      <div className="flex flex-wrap items-center gap-2 border-b border-stroke px-3 py-2 text-xs">
        <WorkerBadge worker={worker} now={now} />
        <span className="min-w-0 flex-1 truncate text-sm font-medium" title={worker.title}>
          {workerTitle(worker)}
        </span>
        <span className="order-last w-full sm:order-none sm:w-auto">
          <WorkerModel worker={worker} />
        </span>
        <button
          type="button"
          className="rounded-control px-2 py-0.5 text-muted hover:bg-raised hover:text-ink"
          onClick={() => dialog.current?.close()}
          aria-label="Close"
        >
          ✕
        </button>
      </div>
      <div className="relative">
        <div
          ref={scroller}
          data-testid="worker-terminal"
          onScroll={e => {
            const bottom = atBottom(e.currentTarget)
            follow.current = bottom
            if (bottom !== following) setFollowing(bottom)
          }}
          className="h-[min(65vh,36rem)] overflow-auto bg-canvas px-3 py-2 font-mono text-[12px] leading-[1.45]"
        >
          {entries.map((entry, i) => (
            <TerminalEntry key={i} entry={entry} />
          ))}
          {!entries.length && <div className="text-muted">{log || !sessionId ? 'No output yet.' : 'Reading…'}</div>}
        </div>
        {!following && (
          <button
            type="button"
            onClick={jumpToEnd}
            className="absolute right-4 bottom-3 rounded-full border border-stroke bg-surface px-2.5 py-1 text-[11px] text-ink shadow hover:bg-raised"
          >
            ↓ Latest
          </button>
        )}
      </div>
      <div className="flex flex-wrap gap-x-2 border-t border-stroke px-3 py-1.5 text-[11px] text-muted">
        <span>{source}</span>
        {live && log?.live && <span>· refreshes every {LOG_REFRESH_MS / 1000}s</span>}
        {(error || log?.error) && <span className="text-danger">· {error || log?.error}</span>}
      </div>
    </dialog>
  )
}
