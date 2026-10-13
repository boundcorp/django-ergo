import { Link } from 'react-router-dom'
import type { Bot, Session, SidebarPin, User } from '../api'
import { BotBadge } from '../components/BotIcon'
import { DashboardWorkspace, DirectoryNotice } from '../components/DashboardWorkspace'
import type { DirectoryState } from '../components/DashboardWorkspace'
import { ago } from '../time'

export function Home({
  user,
  bots,
  sessions,
  pins,
  state,
  onRetry,
}: {
  user: User
  bots: Bot[]
  sessions: Session[]
  pins: Record<string, SidebarPin[]>
  state: DirectoryState
  onRetry: () => void
}) {
  const recent = sessions.slice(0, 4)
  const waiting = sessions.filter(s => s.status !== 'completed' && s.bucket === 'waiting').length
  return (
    <div className="page-content dashboard-page h-full overflow-y-auto">
      <header className="dashboard-header">
        <div>
          <p className="eyebrow">Your workspace</p>
          <h1 className="page-title mt-2">Good to see you, {user.first_name || user.username}</h1>
          <p className="page-lede mt-3">Continue a conversation or give a bot a focused task.</p>
        </div>
        <Link to="/threads" className="dashboard-action dashboard-action-primary">
          Open dashboard <span aria-hidden="true">→</span>
        </Link>
      </header>
      <DirectoryNotice state={state} onRetry={onRetry} />
      <div className="dashboard-layout">
        <section aria-labelledby="recent-title">
          <Link to="/threads" className="surface-card mb-6 block p-4 hover:border-accent">
            <span className="block text-sm font-semibold">
              Threads across your bots <span aria-hidden="true">→</span>
            </span>
            <span className="mt-1 block text-sm text-muted">
              {state === 'loading'
                ? 'Checking for updates…'
                : waiting
                  ? `${waiting} waiting on you. Review approvals, questions and progress.`
                  : 'Review progress, pull requests and all your threads.'}
            </span>
          </Link>
          <div className="mb-4 flex items-center justify-between gap-3">
            <h2 id="recent-title" className="font-display text-xl font-bold">
              Recent conversations
            </h2>
            <Link to="/sessions" className="dashboard-text-link">
              History
            </Link>
          </div>
          {recent.length ? (
            <ul className="space-y-3">
              {recent.map(session => (
                <li key={session.id}>
                  <Link
                    to={`/s/${session.id}`}
                    className={`surface-card flex items-start gap-3 p-4 hover:border-accent ${session.attention ? 'border-warning bg-amber-tint' : session.unread ? 'border-accent' : ''}`}
                  >
                    <BotBadge name={session.bot} size={28} />
                    <span className="min-w-0 flex-1">
                      <span className="block break-words text-sm font-semibold">{session.title}</span>
                      <span className="mt-1 block text-xs text-muted">
                        {session.bot} ·{' '}
                        {session.attention ? 'Action required' : session.busy ? 'Working' : ago(session.updated_at)}
                      </span>
                      {session.unread && !session.attention && (
                        <span className="mt-1 block text-xs font-medium text-accent">Unread reply</span>
                      )}
                    </span>
                    <span aria-hidden="true" className="text-muted">
                      →
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          ) : state === 'ready' ? (
            <div className="surface-card p-6">
              <h3 className="font-semibold">Your first conversation starts here</h3>
              <p className="mt-2 text-sm text-muted">Choose a bot under Start a thread to begin.</p>
              <a href="#dashboard-start" className="dashboard-text-link mt-3 inline-flex">
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
