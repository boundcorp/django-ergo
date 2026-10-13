import { Fragment, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { Link, NavLink, useMatch } from 'react-router-dom'
import type { Session } from '../api'
import { api } from '../api'
import { ago } from '../time'
import { BotBadge } from './BotIcon'
import { PrChip } from './Threads'

// Threads by status: the sidebar's slim list under each bot, and the full Threads page.

type GroupKey = 'pinned' | 'review' | 'waiting' | 'working' | 'idle' | 'resolved'

// open: unfolded at first (sidebar, then full page); capped: shows a few rows, then "Show more".
const GROUPS: { key: GroupKey; label: string; open: [boolean, boolean]; capped?: boolean }[] = [
  { key: 'pinned', label: 'Pinned', open: [true, true] },
  { key: 'review', label: 'Ready for review', open: [true, true] },
  { key: 'waiting', label: 'Waiting on you', open: [true, true] },
  { key: 'working', label: 'Working', open: [true, true] },
  { key: 'idle', label: 'Idle', open: [true, true], capped: true },
  { key: 'resolved', label: 'Resolved', open: [false, true], capped: true },
]

export function groupOf(session: Session): GroupKey {
  if (session.pinned) return 'pinned'
  if (session.bucket) return session.bucket
  return session.status === 'completed' ? 'resolved' : 'idle'
}

// Resolved rows are just a dimmed title; the summary is in the tooltip.
function resolved(session: Session): boolean {
  return session.bucket === 'resolved' || session.status === 'completed'
}

function activity(session: Session): string {
  return session.last_activity || session.updated_at
}

// A chat's state: a spinner while it works, otherwise a dot (attention, unread, delegated, done, open).
export function StatusDot({ session, unread }: { session: Session; unread: boolean }) {
  return session.busy ? (
    <span
      title="Working"
      className="h-2.5 w-2.5 shrink-0 animate-spin rounded-full border-[1.5px] border-indigo-500 border-t-transparent"
    />
  ) : (
    <span
      title={
        session.attention
          ? 'Needs your attention'
          : unread
            ? 'Unread reply'
            : session.open_in
              ? 'Working on a delegated request'
              : session.open_out
                ? 'Waiting on another thread'
                : undefined
      }
      className={`shrink-0 rounded-full ${
        session.attention
          ? 'h-2 w-2 bg-amber-400'
          : unread
            ? 'h-2 w-2 bg-sky-500'
            : session.open_in
              ? 'h-1.5 w-1.5 animate-pulse bg-indigo-400'
              : session.status === 'completed'
                ? 'h-1.5 w-1.5 bg-zinc-400'
                : 'h-1.5 w-1.5 bg-emerald-500'
      }`}
    />
  )
}

const WAITING: Record<string, { word: string; tone: string; fallback: string }> = {
  approval: { word: 'Approval', tone: 'text-warning', fallback: 'Approve or deny a tool call' },
  question: { word: 'Question', tone: 'text-warning', fallback: 'Answer its question' },
  failure: { word: 'Failed', tone: 'text-danger', fallback: 'The last turn stopped; resume or dismiss it' },
}

/** The row's second line: a colored state word when it waits on you, then the bot's status line. */
function StatusLine({ session }: { session: Session }) {
  const waiting = session.bucket === 'waiting' ? WAITING[session.waiting_for || 'question'] : undefined
  let text = session.status_line || waiting?.fallback || ''
  if (!text && session.bucket === 'working') {
    text = session.open_out
      ? `Waiting on ${session.open_out} other thread${session.open_out > 1 ? 's' : ''}`
      : session.workers_running
        ? 'Workers running'
        : 'Working'
  }
  if (!text && !waiting) return null
  return (
    <span className="block text-xs text-muted full-thread-status" title={text}>
      {waiting && <span className={waiting.tone}>{waiting.word}</span>}
      {waiting && text && ' · '}
      {text}
    </span>
  )
}

function useOpenGroups(scope: string) {
  const key = `ergonaut.threads.${scope}`
  const [open, setOpen] = useState<Record<string, boolean>>(() => {
    try {
      return JSON.parse(localStorage.getItem(key) || '{}')
    } catch {
      return {}
    }
  })
  function toggle(group: GroupKey, now: boolean) {
    const next = { ...open, [group]: now }
    setOpen(next)
    try {
      localStorage.setItem(key, JSON.stringify(next))
    } catch {
      // the choice just isn't remembered
    }
  }
  return [open, toggle] as const
}

/** Chats grouped Pinned, Waiting on you, Working, Idle and Resolved, newest activity first. */
export function ThreadGroups({
  sessions,
  scope,
  full,
  onChange,
  after,
}: {
  sessions: Session[]
  scope: string
  full?: boolean
  onChange?: () => void
  after?: (session: Session) => ReactNode // under a slim row (the sidebar shows its pinned files)
}) {
  const [open, toggle] = useOpenGroups(scope)
  const [more, setMore] = useState<Record<string, boolean>>({})
  const cap = full ? 5 : 3
  const grouped = new Map<GroupKey, Session[]>()
  for (const session of sessions) {
    const key = groupOf(session)
    grouped.set(key, [...(grouped.get(key) ?? []), session])
  }
  return (
    <div className={full ? 'space-y-4' : 'space-y-0.5'}>
      {GROUPS.filter(g => grouped.get(g.key)?.length).map(group => {
        const rows = grouped.get(group.key)!.sort((a, b) => activity(b).localeCompare(activity(a)))
        const shown = open[group.key] ?? group.open[full ? 1 : 0]
        const visible = group.capped && !more[group.key] ? rows.slice(0, cap) : rows
        return (
          <section
            key={group.key}
            id={`${scope}-${group.key}`}
            tabIndex={full ? -1 : undefined}
            className={full ? `dashboard-group dashboard-group-${group.key}` : undefined}
            aria-label={full ? group.label : undefined}
          >
            <button
              type="button"
              aria-controls={`${scope}-${group.key}-rows`}
              aria-expanded={shown}
              onClick={() => toggle(group.key, !shown)}
              className={
                full
                  ? 'flex min-h-12 w-full items-center gap-2 px-4 py-3 text-left text-sm font-semibold text-ink'
                  : 'ml-3 flex items-center gap-1 px-2 py-0.5 text-xs font-medium text-muted hover:text-ink'
              }
            >
              <span className="w-3 text-muted">{shown ? '▾' : '▸'}</span>
              {group.label}
              <span
                className={
                  full
                    ? 'ml-auto rounded-full bg-raised px-2 py-0.5 text-xs font-medium text-muted'
                    : 'font-normal text-muted'
                }
              >
                {rows.length}
              </span>
            </button>
            {shown && (
              <div id={`${scope}-${group.key}-rows`} className={full ? 'border-t border-stroke' : ''}>
                {visible.map(session =>
                  full ? (
                    <FullRow key={session.id} session={session} onPinned={onChange} />
                  ) : (
                    <Fragment key={session.id}>
                      <SlimRow session={session} />
                      {after?.(session)}
                    </Fragment>
                  ),
                )}
                {visible.length < rows.length && (
                  <button
                    type="button"
                    onClick={() => setMore(m => ({ ...m, [group.key]: true }))}
                    className={
                      full
                        ? 'min-h-11 w-full border-t border-stroke py-2 text-center text-sm font-medium text-accent hover:bg-raised'
                        : 'ml-6 px-2 py-0.5 text-xs text-muted hover:text-ink'
                    }
                  >
                    Show {rows.length - visible.length} more
                  </button>
                )}
              </div>
            )}
          </section>
        )
      })}
    </div>
  )
}

/** A sidebar row: status dot, title, and the status line under it. */
export function SlimRow({ session }: { session: Session }) {
  // The open chat is being read, so its reply is never shown as unread.
  const open = useMatch(`/s/${session.id}`)
  const unread = !!session.unread && !open
  return (
    <NavLink
      to={`/s/${session.id}`}
      title={
        session.resolved_summary
          ? `Resolved${session.resolved_by ? ` by ${session.resolved_by}` : ''}: ${session.resolved_summary}`
          : undefined
      }
      className={({ isActive }) =>
        `ml-3 flex items-center gap-2 rounded-lg px-3 py-1.5 text-sm ${
          isActive ? 'bg-indigo-tint font-medium text-ink' : 'hover:bg-raised'
        }`
      }
    >
      <StatusDot session={session} unread={unread} />
      <span className="min-w-0 flex-1">
        <span className={`block truncate ${resolved(session) ? 'text-muted' : ''}`}>{session.title}</span>
        {!resolved(session) && <StatusLine session={session} />}
      </span>
      <span className="shrink-0 self-start pt-0.5 text-[11px] font-normal text-muted">
        {shortAgo(activity(session))}
      </span>
    </NavLink>
  )
}

/** A Threads page row: bot, title, status line, PR and worker chips, time, and a pin. */
export function FullRow({ session, onPinned }: { session: Session; onPinned?: () => void }) {
  const [pinned, setPinned] = useState(!!session.pinned)
  const [pinning, setPinning] = useState(false)
  const [pinError, setPinError] = useState(false)
  useEffect(() => setPinned(!!session.pinned), [session.pinned])
  const done = resolved(session)
  return (
    <div className="dashboard-thread">
      <BotBadge name={session.bot} size={26} />
      <Link
        to={`/s/${session.id}`}
        className="dashboard-thread-main"
        title={done ? session.resolved_summary || session.status_line || undefined : undefined}
      >
        <span className={`block break-words text-sm ${done ? 'text-muted' : 'font-semibold text-ink'}`}>
          {session.title}
        </span>
        <span className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted">
          <span>{session.bot}</span>
          {session.unread && <span className="font-medium text-accent">Unread reply</span>}
          {session.busy && <span className="text-teal">Working</span>}
          {done && <span>Resolved</span>}
          <time dateTime={activity(session)} title={new Date(activity(session)).toLocaleString()}>
            {shortAgo(activity(session))}
          </time>
        </span>
        {!done && (
          <span className="mt-1 block">
            <StatusLine session={session} />
          </span>
        )}
      </Link>
      <button
        type="button"
        title={pinned ? 'Unpin' : 'Pin to the top'}
        aria-label={`${pinned ? 'Unpin' : 'Pin'} ${session.title}`}
        aria-pressed={pinned}
        disabled={pinning}
        className={`dashboard-thread-pin ${pinned ? 'text-warning' : 'text-muted'}`}
        onClick={async () => {
          const next = !pinned
          setPinned(next)
          setPinning(true)
          setPinError(false)
          try {
            await api.pinSession(session.id, next)
            onPinned?.()
          } catch {
            setPinned(!next)
            setPinError(true)
          } finally {
            setPinning(false)
          }
        }}
      >
        <span aria-hidden="true">{pinned ? '★' : '☆'}</span>
      </button>
      {!done && (!!session.workers_running || !!session.prs?.length) && (
        <div className="dashboard-thread-evidence">
          {!!session.workers_running && (
            <span
              className="rounded-full border border-stroke px-2 py-1 text-xs text-muted"
              title={`${session.workers_running} of ${session.workers_total} workers still running`}
            >
              {session.workers_running} running · {(session.workers_total ?? 0) - session.workers_running}/
              {session.workers_total} workers done
            </span>
          )}
          {session.prs?.slice(0, 2).map(pr => <PrChip key={pr.url} pr={{ ...pr, title: '' }} />)}
          {(session.prs?.length ?? 0) > 2 && (
            <Link to={`/s/${session.id}`} className="dashboard-text-link text-xs">
              +{session.prs!.length - 2} more PRs<span className="sr-only"> in {session.title}</span>
            </Link>
          )}
        </div>
      )}
      {pinError && (
        <p className="dashboard-thread-evidence text-xs text-danger" role="alert">
          Could not update the pin. Try again.
        </p>
      )}
    </div>
  )
}

// "now", "4m", "15h", "3d": the sidebar has room for little more.
function shortAgo(iso: string): string {
  const text = ago(iso)
  if (text === 'Just now') return 'now'
  if (text === 'Yesterday') return '1d'
  return text.replace(' ago', '')
}
