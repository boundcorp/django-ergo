import { useNavigate, useSearchParams } from 'react-router-dom'
import type { Bot, Session, SidebarPin, User } from '../api'
import { DashboardWorkspace, DirectoryNotice } from '../components/DashboardWorkspace'
import type { DirectoryState } from '../components/DashboardWorkspace'
import { groupOf, ThreadGroups } from '../components/ThreadList'

/** Include all threads and open main/named chats, preserving the dashboard's existing scope. */
export function dashboardSessions(sessions: Session[], bot: string) {
  return sessions.filter(s => (!bot || s.bot === bot) && (s.role === 'thread' || s.status !== 'completed'))
}

export function ThreadsPage({
  user,
  bots,
  sessions,
  pins,
  state,
  onChange,
}: {
  user: User
  bots: Bot[]
  sessions: Session[]
  pins: Record<string, SidebarPin[]>
  state: DirectoryState
  onChange: () => Promise<void>
}) {
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const bot = params.get('bot') || ''
  const shown = dashboardSessions(sessions, bot)
  const waiting = shown.filter(s => s.bucket === 'waiting').length
  const review = shown.filter(s => s.bucket === 'review').length
  const working = shown.filter(s => s.bucket === 'working').length
  const unread = shown.filter(s => s.unread).length
  return (
    <div className="page-content dashboard-page h-full overflow-y-auto">
      <header className="dashboard-header">
        <div className="min-w-0">
          <p className="eyebrow">{user.first_name || user.username}’s workspace</p>
          <h1 className="page-title mt-2">Threads</h1>
          <p className="page-lede mt-3">
            {state === 'loading'
              ? 'Checking your threads…'
              : state === 'error' && !sessions.length
                ? 'Workspace updates are unavailable.'
                : waiting
                  ? `${waiting} thread${waiting > 1 ? 's need' : ' needs'} your input.`
                  : 'Nothing is waiting on you.'}
            {review ? ` ${review} ready for review.` : ''}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button type="button" onClick={() => navigate(-1)} className="dashboard-action">
            Back
          </button>
          <a href="#dashboard-start" className="dashboard-action dashboard-action-primary">
            New thread <span aria-hidden="true">+</span>
          </a>
        </div>
      </header>
      <DirectoryNotice state={state} onRetry={() => onChange().catch(() => {})} />
      {(state === 'ready' || shown.length > 0) && (
        <nav className="dashboard-overview" aria-label="Jump to loaded thread activity">
          {[
            {
              count: waiting,
              label: 'Waiting on you',
              tone: 'text-warning',
              first: shown.find(s => s.bucket === 'waiting'),
            },
            {
              count: review,
              label: 'Ready for review',
              tone: 'text-accent',
              first: shown.find(s => s.bucket === 'review'),
            },
            { count: working, label: 'Working', tone: 'text-teal', first: shown.find(s => s.bucket === 'working') },
            { count: unread, label: 'Unread replies', tone: 'text-ink', first: shown.find(s => s.unread) },
          ].map(({ count, label, tone, first }) =>
            first ? (
              <a key={label} href={`#page-${groupOf(first)}`} className="dashboard-overview-item hover:bg-raised">
                <span className={`font-display text-lg font-bold ${tone}`}>{count}</span>
                <span className="text-xs text-muted">{label}</span>
                <span aria-hidden="true" className="ml-auto text-muted">
                  ↓
                </span>
              </a>
            ) : (
              <span key={label} className="dashboard-overview-item">
                <span className={`font-display text-lg font-bold ${tone}`}>{count}</span>
                <span className="text-xs text-muted">{label}</span>
              </span>
            ),
          )}
        </nav>
      )}
      <div className="dashboard-layout">
        <section aria-labelledby="dashboard-threads-title" className="min-w-0">
          <div className="mb-4 flex flex-wrap items-center gap-3">
            <h2 id="dashboard-threads-title" className="font-display text-lg font-bold">
              {bot || 'All bots'} <span className="text-sm font-normal text-muted">· {shown.length} conversations</span>
            </h2>
          </div>
          <div className="dashboard-filters mb-5" role="group" aria-label="Filter threads by bot">
            {['', ...bots.map(b => b.name)].map(choice => (
              <button
                key={choice || 'all'}
                type="button"
                aria-pressed={bot === choice}
                onClick={() => setParams(choice ? { bot: choice } : {})}
                className={`dashboard-filter ${bot === choice ? 'border-accent bg-indigo-tint text-accent-soft' : 'border-stroke text-muted hover:bg-raised'}`}
              >
                {choice || 'All bots'}
              </button>
            ))}
          </div>
          {shown.length ? (
            <ThreadGroups sessions={shown} scope="page" full onChange={() => onChange().catch(() => {})} />
          ) : state === 'ready' ? (
            <div className="surface-card p-6">
              <h3 className="font-semibold">{bot ? `No threads with ${bot} yet` : 'A clear workspace'}</h3>
              <p className="mt-2 text-sm text-muted">
                Start a thread with a bot. Its progress, questions and pull requests will appear here.
              </p>
              <a href="#dashboard-start" className="dashboard-text-link mt-4 inline-flex">
                Choose a bot →
              </a>
            </div>
          ) : null}
        </section>
        {(bots.length > 0 || state === 'ready') && <DashboardWorkspace bots={bots} sessions={sessions} pins={pins} />}
      </div>
    </div>
  )
}
