import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import type { Bot, Session } from '../api'
import { api } from '../api'
import { ago } from '../time'

export function Sessions({ bots }: { bots: Bot[] }) {
  const [q, setQ] = useState('')
  const [bot, setBot] = useState('')
  const [status, setStatus] = useState('')
  const [sessions, setSessions] = useState<Session[] | null>(null)

  useEffect(() => {
    const timer = setTimeout(() => api.sessions({ q, bot, status }).then(setSessions), 250)
    return () => clearTimeout(timer)
  }, [q, bot, status])

  const field =
    'rounded-card border border-stroke bg-raised px-4 py-3 text-sm text-ink focus-visible:outline-2 focus-visible:outline-accent'

  return (
    <div className="page-content h-full overflow-y-auto">
      <h1 className="page-title">Session history</h1>
      <p className="page-lede mt-3">
        Search message text, filter by bot or status, then open a session from its title.
      </p>
      <div className="mb-6 mt-6 flex flex-wrap gap-4">
        <input
          value={q}
          onChange={e => setQ(e.target.value)}
          placeholder="Search message text"
          aria-label="Search message text"
          className={`${field} min-w-60 flex-1`}
        />
        <select
          value={bot}
          onChange={e => setBot(e.target.value)}
          aria-label="Filter by bot"
          className={`${field} min-w-40`}
        >
          <option value="">All bots</option>
          {bots.map(b => (
            <option key={b.name}>{b.name}</option>
          ))}
        </select>
        <div className="flex rounded-card border border-stroke bg-raised p-1" aria-label="Filter by status">
          {[
            ['', 'Any status'],
            ['active', 'Active'],
            ['completed', 'Resolved'],
          ].map(([value, label]) => (
            <button
              key={value}
              type="button"
              aria-pressed={status === value}
              onClick={() => setStatus(value)}
              className={`rounded-control px-3 py-2 text-xs font-semibold ${
                status === value ? 'bg-indigo-tint text-accent-soft' : 'text-muted hover:bg-surface'
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
      {sessions === null ? (
        <p className="text-zinc-500">Loading…</p>
      ) : !sessions.length ? (
        <p className="text-zinc-500">No sessions match.</p>
      ) : (
        <div className="data-scroll rounded-card border border-stroke">
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-zinc-500">
              <tr>
                <th>Session</th>
                <th>Bot</th>
                <th>Kind</th>
                <th>Person</th>
                <th>Status</th>
                <th>Last active</th>
              </tr>
            </thead>
            <tbody>
              {sessions.map(s => (
                <tr key={s.id} className="border-t border-zinc-200 dark:border-zinc-800">
                  <td>
                    <Link className="text-accent hover:underline" to={`/s/${s.id}`}>
                      {s.title}
                    </Link>
                  </td>
                  <td>{s.bot}</td>
                  <td className="capitalize">{s.role || '-'}</td>
                  <td className="capitalize">{s.username}</td>
                  <td className={s.status === 'completed' ? 'status-closed' : 'status-active'}>
                    {s.status === 'completed' ? (s.role === 'thread' ? 'Resolved' : 'Closed') : 'Active'}
                  </td>
                  <td>
                    <time dateTime={s.updated_at} title={new Date(s.updated_at).toLocaleString()}>
                      {ago(s.updated_at)}
                    </time>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
