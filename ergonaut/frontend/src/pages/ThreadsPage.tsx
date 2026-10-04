import { useNavigate, useSearchParams } from 'react-router-dom'
import type { Bot, Session, User } from '../api'
import { ThreadGroups } from '../components/ThreadList'

/** Every chat across bots, grouped by what it needs: the sidebar's threads, popped out. */
export function ThreadsPage({
  user,
  bots,
  sessions,
  onChange,
}: {
  user: User
  bots: Bot[]
  sessions: Session[]
  onChange: () => void
}) {
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const bot = params.get('bot') || ''
  // Main and named chats count too while open; closed ones live in History.
  const shown = sessions.filter(s => (!bot || s.bot === bot) && (s.role === 'thread' || s.status !== 'completed'))
  const waiting = shown.filter(s => s.bucket === 'waiting').length
  const name = user.first_name || user.username
  return (
    <div className="page-content h-full overflow-y-auto">
      <div className="flex items-start gap-4">
        <div className="min-w-0 flex-1">
          <p className="eyebrow">Threads{bot && ` · ${bot}`}</p>
          <h1 className="page-title mt-3">Welcome back, {name}</h1>
          <p className="page-lede mt-3">
            {waiting
              ? `${waiting} thread${waiting > 1 ? 's are' : ' is'} waiting on you.`
              : 'Nothing is waiting on you.'}
          </p>
        </div>
        <button
          type="button"
          onClick={() => navigate(-1)}
          className="mt-1 text-sm text-muted hover:text-ink"
          title="Back"
        >
          ✕
        </button>
      </div>
      <div className="mt-6 flex flex-wrap gap-2">
        {['', ...bots.map(b => b.name)].map(choice => (
          <button
            key={choice || 'all'}
            type="button"
            aria-pressed={bot === choice}
            onClick={() => setParams(choice ? { bot: choice } : {})}
            className={`rounded-control px-3 py-1.5 text-xs font-semibold ${
              bot === choice ? 'bg-indigo-tint text-accent-soft' : 'text-muted hover:bg-raised'
            }`}
          >
            {choice || 'All bots'}
          </button>
        ))}
      </div>
      <div className="mt-6">
        {shown.length ? (
          <ThreadGroups sessions={shown} scope="page" full onChange={onChange} />
        ) : (
          <p className="text-sm text-muted">No threads yet.</p>
        )}
      </div>
    </div>
  )
}
