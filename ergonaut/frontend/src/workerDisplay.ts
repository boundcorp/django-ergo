import type { Worker, WorkerAgent, WorkerEntry } from './api'

// Pure helpers for showing a chat's workers: their state, the model they run on and a clean
// line of their latest output.

// CSI/OSC escape sequences and the remaining C0 controls (tabs become spaces).
// eslint-disable-next-line no-control-regex
const ANSI = /\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])/g
// eslint-disable-next-line no-control-regex
const CONTROL = /[\x00-\x08\x0b-\x1f\x7f]/g

/** Text without terminal escape codes or control characters; line breaks kept. */
export function stripAnsi(text: string): string {
  return text.replace(ANSI, '').replace(/\r\n?/g, '\n').replace(/\t/g, '  ').replace(CONTROL, '')
}

/** One display line: escapes stripped, every run of whitespace (line breaks too) one space. */
export function cleanLine(text: string): string {
  return stripAnsi(text).replace(/\s+/g, ' ').trim()
}

/** Whether a line says something: TUI borders, spinners and prompts don't. */
function meaningful(line: string): boolean {
  return (line.match(/[\p{L}\p{N}]/gu)?.length ?? 0) >= 3
}

const LINE_PREFIX: Record<string, string> = { error: '✗ ', reasoning: '… ' }

/** The latest line of a worker's output worth showing in one row, or ''. */
export function latestLine(entries: WorkerEntry[] | undefined): string {
  for (let i = (entries?.length ?? 0) - 1; i >= 0; i--) {
    const entry = entries![i]
    // A long result is the tail of something; its last line is the freshest.
    const lines = stripAnsi(entry.text).split('\n').map(cleanLine).filter(meaningful)
    if (lines.length) return (LINE_PREFIX[entry.kind] ?? '') + lines[lines.length - 1]
  }
  return ''
}

const CLAUDE_MODEL = /^claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$/i

/** A model id as people say it: "claude-haiku-5-5" is "Haiku 5.5"; others as given. */
export function modelName(model: string): string {
  const id = model.includes('/') ? model.slice(model.indexOf('/') + 1) : model
  const claude = CLAUDE_MODEL.exec(id)
  if (claude) {
    const family = claude[1][0].toUpperCase() + claude[1].slice(1).toLowerCase()
    return `${family} ${claude[2]}${claude[3] ? `.${claude[3]}` : ''}`
  }
  return id
}

/** The model a worker's agent actually runs on (the launch model when seen), else ''. */
export function workerModel(agent: WorkerAgent | null | undefined): string {
  return agent?.launch_model || agent?.model || ''
}

/** "Haiku 5.5 · low" (or "model unknown"), with the agent first when asked for. */
export function agentLabel(agent: WorkerAgent, withAgent = true): string {
  const model = workerModel(agent)
  return [withAgent ? agent.agent : '', model ? modelName(model) : 'model unknown', agent.effort || agent.tier]
    .filter(Boolean)
    .join(' · ')
}

/** Every recorded detail, for a tooltip. */
export function agentDetail(agent: WorkerAgent): string {
  return [
    agent.agent && `Agent: ${agent.agent}`,
    `Model: ${agent.model || 'unknown'}`,
    agent.launch_model && agent.launch_model !== agent.model && `Launched on: ${agent.launch_model}`,
    agent.effort && `Effort: ${agent.effort}`,
    agent.tier && `Tier: ${agent.tier}`,
  ]
    .filter(Boolean)
    .join('\n')
}

/** The title without the "claude: " the agent launcher puts in front (shown separately). */
export function workerTitle(worker: Worker): string {
  const agent = worker.agent?.agent
  return agent && worker.title.startsWith(`${agent}: `) ? worker.title.slice(agent.length + 2) : worker.title
}

/** A length of time, short: "4m", "2h 5m", "3d" (under a minute is "<1m"). */
export function span(seconds: number): string {
  if (seconds < 60) return '<1m'
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`
  return `${Math.floor(seconds / 86_400)}d`
}

export type WorkerState = 'queued' | 'running' | 'waiting' | 'stalled' | 'done' | 'needs_run' | 'failed' | 'cancelled'

export const WORKER_STATE: Record<WorkerState, { label: string; badge: string }> = {
  queued: { label: 'Queued', badge: 'bg-raised text-muted' },
  running: { label: 'Running', badge: 'bg-indigo-tint text-accent' },
  waiting: { label: 'Waiting on you', badge: 'bg-amber-tint text-warning' },
  stalled: { label: 'Stalled', badge: 'bg-amber-tint text-warning' },
  done: { label: 'Done', badge: 'bg-teal-tint text-success' },
  needs_run: { label: 'Needs new run', badge: 'bg-amber-tint text-warning' },
  failed: { label: 'Failed', badge: 'bg-red-tint text-danger' },
  cancelled: { label: 'Cancelled', badge: 'bg-raised text-muted' },
}

/** Whether a running worker has gone quiet for longer than its plugin allows. */
export function isStalled(worker: Worker, now: number): boolean {
  const activity = worker.activity
  if (worker.status !== 'running' || !activity?.at || !activity.stall_after || activity.waiting) return false
  return now - activity.at > activity.stall_after
}

/** What a worker is doing now, for its badge (a person's question beats a stall). */
export function workerState(worker: Worker, now: number): WorkerState {
  if (worker.status === 'running') {
    if (worker.activity?.waiting) return 'waiting'
    return isStalled(worker, now) ? 'stalled' : 'running'
  }
  if (worker.status === 'completed') {
    const result = worker.result
    const needsRun =
      result != null && typeof result === 'object' && 'status' in result && result.status === 'needs_new_run'
    return needsRun ? 'needs_run' : 'done'
  }
  return worker.status
}

export function isLive(worker: Worker): boolean {
  return worker.status === 'queued' || worker.status === 'running'
}

/** How long it has run (to now, or to when it finished), in seconds; null without a start. */
export function elapsed(worker: Worker, now: number): number | null {
  const start = Date.parse(worker.created_at)
  if (Number.isNaN(start)) return null
  const end = worker.completed_at ? Date.parse(worker.completed_at) : now * 1000
  return Math.max(0, (end - start) / 1000)
}

/** The activity panel's one-line summary: "4 agents running · 1 waiting on you · 2 done". */
export function workersSummary(workers: Worker[], now: number): string {
  const counts: Partial<Record<WorkerState, number>> = {}
  for (const worker of workers) {
    const state = workerState(worker, now)
    counts[state] = (counts[state] ?? 0) + 1
  }
  const noun = (n: number) =>
    workers.every(w => w.agent) ? `agent${n === 1 ? '' : 's'}` : `worker${n === 1 ? '' : 's'}`
  const running = counts.running ?? 0
  const finished = (counts.done ?? 0) + (counts.needs_run ?? 0) + (counts.cancelled ?? 0)
  return [
    running ? `${running} ${noun(running)} running` : '',
    counts.waiting ? `${counts.waiting} waiting on you` : '',
    counts.stalled ? `${counts.stalled} stalled` : '',
    counts.queued ? `${counts.queued} queued` : '',
    counts.failed ? `${counts.failed} failed` : '',
    finished ? `${finished} finished` : '',
  ]
    .filter(Boolean)
    .join(' · ')
}

// Which worker to show first in a one-line strip: the ones that need a person, then trouble,
// then work in progress, then the finished.
const STATE_ORDER: WorkerState[] = [
  'waiting',
  'failed',
  'stalled',
  'needs_run',
  'running',
  'queued',
  'done',
  'cancelled',
]

/** Workers in strip order (by state, then oldest first), without changing the list. */
export function stripOrder(workers: Worker[], now: number): Worker[] {
  const rank = (w: Worker) => STATE_ORDER.indexOf(workerState(w, now))
  return workers
    .map((worker, i) => ({ worker, i, rank: rank(worker) }))
    .sort((a, b) => a.rank - b.rank || a.i - b.i)
    .map(x => x.worker)
}

/** The short model a chip shows ("Haiku 5.5", "gpt-5.2-codex"), or ''. */
export function chipModel(worker: Worker): string {
  if (!worker.agent) return ''
  const model = workerModel(worker.agent)
  return model ? modelName(model) : '?'
}

/** "4 agents", "1 worker": the strip's leading count. */
export function workersCount(workers: Worker[]): string {
  const noun = workers.length && workers.every(w => w.agent) ? 'agent' : 'worker'
  return `${workers.length} ${noun}${workers.length === 1 ? '' : 's'}`
}

/**
 * Titles for the strip's badges: when every worker's title starts with the same "Topic: ", the
 * badges drop it so the part that tells them apart survives truncation.
 */
export function chipTitles(workers: Worker[]): Map<string, string> {
  const titles = workers.map(w => [w.id, workerTitle(w)] as const)
  const prefix = /^([^:]{1,40}): \S/.exec(titles[0]?.[1] ?? '')?.[1]
  const shared = !!prefix && titles.length > 1 && titles.every(([, t]) => t.startsWith(`${prefix}: `))
  return new Map(titles.map(([id, t]) => [id, shared ? t.slice(prefix!.length + 2) : t]))
}
