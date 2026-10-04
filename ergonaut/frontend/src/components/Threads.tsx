import { useContext } from 'react'
import { Link } from 'react-router-dom'
import type { PrLink, SentCard, Session, ThreadSummary, Worker } from '../api'
import { BotBadge, DirectoryContext } from './BotIcon'

const PR_TONE: Record<PrLink['state'], string> = {
  '': 'border-stroke text-muted',
  open: 'border-success/50 text-success',
  draft: 'border-stroke text-muted',
  merged: 'border-accent/60 text-accent',
  closed: 'border-danger/50 text-danger',
}
const CHECKS: Record<PrLink['checks'], string> = { '': '', passing: '✓', failing: '✗', pending: '…' }

/** A pull request with its live state (and CI), opening it on GitHub. */
export function PrChip({ pr }: { pr: PrLink }) {
  const repo = pr.repo.split('/').pop()
  return (
    <a
      href={pr.url}
      target="_blank"
      rel="noreferrer"
      title={[pr.title, pr.state || 'state not read yet', pr.checks && `checks ${pr.checks}`]
        .filter(Boolean)
        .join(' · ')}
      onClick={e => e.stopPropagation()}
      className={`inline-flex max-w-full items-center gap-1 rounded-full border px-2 py-0.5 text-xs ${PR_TONE[pr.state]}`}
    >
      <span>⑂</span>
      <span className="shrink-0 font-medium">
        {repo}#{pr.number}
      </span>
      {pr.title && <span className="truncate text-muted">{pr.title}</span>}
      <span className="shrink-0">{pr.state || '…'}</span>
      {pr.checks && (
        <span className={pr.checks === 'failing' ? 'text-danger' : pr.checks === 'passing' ? 'text-success' : ''}>
          {CHECKS[pr.checks]}
        </span>
      )}
    </a>
  )
}

const CARD_STATUS: Record<SentCard['status'], { label: string; tone: string }> = {
  queued: { label: 'Queued', tone: 'text-muted' },
  working: { label: 'Working', tone: 'text-accent' },
  waiting: { label: 'Waiting on you', tone: 'text-warning' },
  done: { label: 'Done', tone: 'text-success' },
  failed: { label: 'Failed', tone: 'text-danger' },
}

function Spinner() {
  return (
    <span className="h-2.5 w-2.5 shrink-0 animate-spin rounded-full border-[1.5px] border-indigo-500 border-t-transparent" />
  )
}

// The other chat's own state, when it adds something to the request's status.
function threadNote(thread: ThreadSummary): string {
  if (thread.state === 'waiting_for_approval') return 'needs an approval'
  if (thread.attention) return 'waiting on you'
  if (thread.workers_running) return `${thread.workers_running} worker${thread.workers_running > 1 ? 's' : ''} running`
  if (thread.waiting_on) return `waiting on ${thread.waiting_on} other chat${thread.waiting_on > 1 ? 's' : ''}`
  if (thread.archived) return thread.resolved_by ? `resolved by ${thread.resolved_by}` : 'resolved'
  if (thread.ready_to_resolve) return 'ready to resolve'
  return ''
}

/** Work this chat sent to another chat: where it went, how it's going, and what came out. Opens that chat. */
export function ThreadCard({ card }: { card: SentCard }) {
  const status = CARD_STATUS[card.status]
  const note = threadNote(card.thread)
  return (
    <Link
      to={`/s/${card.thread.id}`}
      className="block w-full rounded-card border border-teal/40 bg-teal-tint px-3 py-2 text-sm hover:border-teal"
    >
      <div className="flex items-center gap-2">
        <BotBadge name={card.thread.bot} size={20} />
        <span className="truncate font-medium">{card.thread.title}</span>
        <span className="shrink-0 text-xs text-muted">{card.thread.bot}</span>
        <span className={`ml-auto flex shrink-0 items-center gap-1.5 text-xs ${status.tone}`}>
          {card.status === 'working' && <Spinner />}
          {status.label}
          {note && <span className="text-muted">· {note}</span>}
        </span>
      </div>
      <div className="mt-1 truncate text-xs text-muted" title={card.text}>
        → {card.text}
      </div>
      {card.thread.archived && card.thread.resolved_summary && (
        <div className="mt-1 line-clamp-2 text-xs text-success" title={card.thread.resolved_summary}>
          ✓ {card.thread.resolved_summary}
        </div>
      )}
      {card.reply && card.status !== 'working' && card.status !== 'queued' && (
        <div className="mt-1 line-clamp-2 text-xs text-ink" title={card.reply}>
          ↩ {card.reply}
        </div>
      )}
      {!!card.prs.length && (
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {card.prs.map(pr => (
            <PrChip key={pr.url} pr={pr} />
          ))}
        </div>
      )}
    </Link>
  )
}

const WORKER_STATUS: Record<Worker['status'], { label: string; tone: string }> = {
  queued: { label: 'Queued', tone: 'text-muted' },
  running: { label: 'Running', tone: 'text-accent' },
  completed: { label: 'Done', tone: 'text-success' },
  failed: { label: 'Failed', tone: 'text-danger' },
  cancelled: { label: 'Cancelled', tone: 'text-muted' },
}

/** A worker this chat started (orca_start_worker, ergo_workers_start): status, progress and PRs. */
export function WorkerCard({ worker }: { worker: Worker }) {
  const status = WORKER_STATUS[worker.status] ?? WORKER_STATUS.queued
  return (
    <div className="w-full rounded-card border border-stroke bg-surface px-3 py-2 text-sm">
      <div className="flex items-center gap-2">
        <span>⚙</span>
        <span className="truncate font-medium">{worker.title}</span>
        <span className={`ml-auto flex shrink-0 items-center gap-1.5 text-xs ${status.tone}`}>
          {worker.status === 'running' && <Spinner />}
          {status.label}
        </span>
      </div>
      {(worker.error || worker.progress) && (
        <div className="mt-1 line-clamp-2 text-xs text-muted">{worker.error || worker.progress}</div>
      )}
      {!!worker.prs?.length && (
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {worker.prs.map(pr => (
            <PrChip key={pr.url} pr={pr} />
          ))}
        </div>
      )}
    </div>
  )
}

function dotFor(session: Pick<Session, 'busy' | 'attention' | 'status'>): string {
  if (session.busy) return 'bg-indigo-500 animate-pulse'
  if (session.attention) return 'bg-amber-400'
  return session.status === 'completed' ? 'bg-zinc-400' : 'bg-emerald-500'
}

/** A link to another chat, named in a bot's text, with its status dot. */
export function ThreadLink({ id, children }: { id: string; children?: React.ReactNode }) {
  const { sessions } = useContext(DirectoryContext)
  const session = sessions.find(s => s.id === id)
  return (
    <Link
      to={`/s/${id}`}
      className="inline-flex items-center gap-1 rounded-full border border-teal/40 bg-teal-tint px-1.5 align-baseline text-[0.9em] text-ink no-underline hover:border-teal"
    >
      {session && <span className={`h-1.5 w-1.5 rounded-full ${dotFor(session)}`} />}
      {children || session?.title || 'thread'}
    </Link>
  )
}

const UUID = /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/g

/** Markdown with the ids of known chats turned into links (Markdown renders /s/ links as ThreadLinks). */
export function linkThreads(text: string, known: Map<string, string>): string {
  return text.replace(UUID, (id, offset: number, whole: string) => {
    if (!known.has(id)) return id
    // Leave ids already inside a link or code alone.
    const before = whole.slice(Math.max(0, offset - 2), offset)
    if (before.endsWith('(') || before.endsWith('/') || before.endsWith('`')) return id
    return `[${known.get(id)!.replace(/[[\]]/g, '')}](/s/${id})`
  })
}
