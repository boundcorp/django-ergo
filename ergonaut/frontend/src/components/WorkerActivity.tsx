import { useEffect, useState } from 'react'
import type { Worker, WorkerEntry, WorkerLog } from '../api'
import { api } from '../api'

// What a worker's agent is doing: its latest output, how long since it last did something
// (flagged as stalled past the plugin's stall_after), and its full recent log on demand.

const LOG_REFRESH_MS = 10_000

/** The current time in epoch seconds, ticking every `ms`. */
function useNow(ms = 15_000): number {
  const [now, setNow] = useState(() => Date.now() / 1000)
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), ms)
    return () => clearInterval(timer)
  }, [ms])
  return now
}

/** A length of time, short: "4m", "2h 5m", "3d" (under a minute is "<1m"). */
function span(seconds: number): string {
  if (seconds < 60) return '<1m'
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`
  return `${Math.floor(seconds / 86_400)}d`
}

const KIND: Record<string, { mark: string; tone: string }> = {
  tool: { mark: '▸', tone: 'text-accent' },
  result: { mark: '↳', tone: 'text-muted' },
  error: { mark: '✗', tone: 'text-danger' },
  reasoning: { mark: '…', tone: 'text-muted italic' },
  user: { mark: '›', tone: 'text-muted' },
  terminal: { mark: '$', tone: 'text-muted' },
}

function Entry({ entry, full }: { entry: WorkerEntry; full?: boolean }) {
  const kind = KIND[entry.kind] ?? { mark: '•', tone: '' }
  return (
    <div className={`flex gap-1.5 font-mono text-[11px] leading-snug ${kind.tone}`}>
      <span className="shrink-0 select-none">{kind.mark}</span>
      <span className={full ? 'whitespace-pre-wrap break-words' : 'truncate'}>{entry.text}</span>
    </div>
  )
}

/** Whether a running worker has gone quiet for longer than its plugin allows. */
export function isStalled(worker: Worker, now: number): boolean {
  const activity = worker.activity
  if (worker.status !== 'running' || !activity?.at || !activity.stall_after || activity.waiting) return false
  return now - activity.at > activity.stall_after
}

/** When the worker last did something, and whether it's stalled or waiting on a person. */
export function WorkerPulse({ worker }: { worker: Worker }) {
  const now = useNow()
  const activity = worker.activity
  if (!activity || worker.status !== 'running') return null
  if (activity.waiting)
    return (
      <span className="shrink-0 rounded-full bg-amber-100 px-1.5 text-[11px] text-amber-800 dark:bg-amber-900/40 dark:text-amber-200">
        waiting on you: {activity.waiting}
      </span>
    )
  if (!activity.at) return null
  const stalled = isStalled(worker, now)
  return (
    <span
      className={`shrink-0 text-[11px] ${stalled ? 'rounded-full bg-amber-100 px-1.5 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200' : 'text-muted'}`}
      title={activity.checked_at ? `Orca checked ${span(now - activity.checked_at)} ago` : undefined}
    >
      {stalled ? `stalled · no output for ${span(now - activity.at)}` : `active ${span(now - activity.at)} ago`}
    </span>
  )
}

/** The worker's latest few lines of output, and a button that opens its full recent log. */
export function WorkerActivityView({ worker, lines = 4 }: { worker: Worker; lines?: number }) {
  const [open, setOpen] = useState(false)
  const entries = worker.activity?.entries ?? []
  const canLog = !!worker.session_id && worker.function.startsWith('orca:')
  if (!entries.length && !canLog) return null
  return (
    <div className="mt-1.5">
      {!open && !!entries.length && (
        <div className="flex flex-col gap-0.5 rounded border border-stroke/60 bg-black/[0.03] px-2 py-1 dark:bg-white/[0.03]">
          {entries.slice(-lines).map((entry, i) => (
            <Entry key={i} entry={entry} />
          ))}
        </div>
      )}
      {canLog && (
        <button
          className="mt-1 text-[11px] text-muted hover:text-ink"
          onClick={e => {
            e.stopPropagation()
            setOpen(o => !o)
          }}
        >
          {open ? '▾ Hide log' : '▸ Open full log'}
        </button>
      )}
      {open && canLog && <WorkerLogView sessionId={worker.session_id!} worker={worker} />}
    </div>
  )
}

/** The worker's recent output, read now and refreshed while it runs. */
function WorkerLogView({ sessionId, worker }: { sessionId: string; worker: Worker }) {
  const [log, setLog] = useState<WorkerLog | null>(null)
  const [error, setError] = useState('')
  const running = worker.status === 'running' || worker.status === 'queued'
  useEffect(() => {
    let alive = true
    const load = () =>
      api
        .workerLog(sessionId, worker.id)
        .then(found => {
          if (!alive) return
          setLog(found)
          setError('')
        })
        .catch(e => alive && setError(String(e.message ?? e)))
    load()
    const timer = running ? setInterval(load, LOG_REFRESH_MS) : undefined
    return () => {
      alive = false
      if (timer) clearInterval(timer)
    }
  }, [sessionId, worker.id, running])
  return (
    <div className="mt-1 rounded border border-stroke bg-black/[0.03] px-2 py-1.5 dark:bg-white/[0.03]">
      <div className="mb-1 flex gap-2 text-[11px] text-muted">
        <span>{log ? `${log.source || 'output'}${log.live ? '' : ' · as of the last check'}` : 'Reading…'}</span>
        {running && log?.live && <span>· refreshes every {LOG_REFRESH_MS / 1000}s</span>}
      </div>
      {(error || log?.error) && <div className="mb-1 text-[11px] text-danger">{error || log?.error}</div>}
      <div className="flex max-h-96 flex-col gap-1 overflow-y-auto">
        {log?.entries.map((entry, i) => <Entry key={i} entry={entry} full />)}
        {log && !log.entries.length && <div className="text-[11px] text-muted">No output yet.</div>}
      </div>
    </div>
  )
}
