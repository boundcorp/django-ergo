import { Link } from 'react-router-dom'
import type { Bot, Session, SidebarPin } from '../api'
import { BotBadge } from './BotIcon'
import { pinIcon } from './Pins'

export type DirectoryState = 'loading' | 'ready' | 'error'

export function DirectoryNotice({ state, onRetry }: { state: DirectoryState; onRetry: () => void }) {
  if (state === 'ready') return null
  return (
    <div
      className="surface-card mb-5 flex flex-wrap items-center gap-3 p-4"
      role={state === 'error' ? 'alert' : 'status'}
    >
      <p className="flex-1 text-sm text-muted">
        {state === 'loading'
          ? 'Loading your workspace…'
          : 'Could not refresh your workspace. Previously loaded items remain visible.'}
      </p>
      {state === 'error' && (
        <button type="button" className="dashboard-action" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  )
}

/** Existing launch and pinned-file actions, reachable without the phone drawer. */
export function DashboardWorkspace({
  bots,
  sessions,
  pins,
}: {
  bots: Bot[]
  sessions: Session[]
  pins: Record<string, SidebarPin[]>
}) {
  const pinned = sessions.flatMap(session => (pins[session.id] || []).map(pin => ({ session, pin })))
  return (
    <aside className="dashboard-workspace space-y-5" aria-label="Workspace shortcuts">
      <section className="surface-card p-4" id="dashboard-start" tabIndex={-1} aria-labelledby="dashboard-start-title">
        <h2 id="dashboard-start-title" className="font-display text-lg font-bold">
          Start a thread
        </h2>
        <p className="mt-1 text-xs text-muted">Choose a bot for your next task.</p>
        {bots.length ? (
          <ul className="mt-3 space-y-2">
            {bots.map(bot => (
              <li key={bot.name}>
                <Link to={`/bots/${bot.name}/new-thread`} className="dashboard-bot-link">
                  <BotBadge name={bot.name} size={28} />
                  <span className="min-w-0 flex-1">
                    <span className="block break-words text-sm font-semibold">{bot.name}</span>
                    {bot.description && (
                      <span className="mt-0.5 block line-clamp-2 text-xs text-muted">{bot.description}</span>
                    )}
                  </span>
                  <span className="text-accent" aria-hidden="true">
                    +
                  </span>
                  <span className="sr-only">New thread</span>
                </Link>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-3 text-sm text-muted">No bots are loaded. Set ERGONAUT_BOTS and restart.</p>
        )}
      </section>
      <section className="surface-card p-4" aria-labelledby="dashboard-pins-title">
        <h2 id="dashboard-pins-title" className="font-display text-lg font-bold">
          Pinned files & pages
        </h2>
        {pinned.length ? (
          <ul className="mt-3 space-y-1">
            {pinned.map(({ session, pin }) => (
              <li key={`${session.id}:${pin.url}`}>
                <Link
                  className="dashboard-pin-link"
                  to={`/s/${session.id}?pin=${encodeURIComponent(pin.url)}&name=${encodeURIComponent(pin.name)}`}
                >
                  <span aria-hidden="true">{pinIcon(pin)}</span>
                  <span className="min-w-0">
                    <span className="block break-words text-sm font-medium">{pin.name}</span>
                    <span className="block truncate text-xs text-muted">
                      {session.bot} · {session.title}
                    </span>
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-3 text-sm text-muted">Pin a file or page in a chat to keep it here.</p>
        )}
      </section>
    </aside>
  )
}
