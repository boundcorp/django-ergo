import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import type { Bot, Session } from '../api'
import { api } from '../api'

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
      <p className="page-lede mt-2 mb-6">
        Search message text, filter by bot or status, then open a session from its title.
      </p>
      <div className="mb-6 flex flex-wrap gap-4">
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
          aria-label="Bot"
          className={`${field} min-w-40`}
        >
          <option value="">All bots</option>
          {bots.map(b => (
            <option key={b.name}>{b.name}</option>
          ))}
        </select>
        <select
          value={status}
          onChange={e => setStatus(e.target.value)}
          aria-label="Status"
          className={`${field} min-w-40`}
        >
          <option value="">Any status</option>
          <option value="active">Active</option>
          <option value="completed">Closed</option>
        </select>
      </div>
      {sessions === null ? (
        <p className="text-muted">Loading…</p>
      ) : !sessions.length ? (
        <p className="text-muted">No sessions match.</p>
      ) : (
        <div className="data-scroll rounded-card border border-stroke bg-surface">
          <table className="w-full text-sm text-ink">
            <thead className="text-left text-xs font-semibold tracking-wide text-muted">
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
                <tr key={s.id}>
                  <td className="border-t border-stroke">
                    <Link
                      className="text-accent hover:underline focus-visible:underline"
                      to={`/s/${s.id}`}
                    >
                      {s.title}
                    </Link>
                  </td>
                  <td className="border-t border-stroke">{s.bot}</td>
                  <td className="border-t border-stroke">{s.role || '-'}</td>
                  <td className="border-t border-stroke">{s.username}</td>
                  <td className="border-t border-stroke">
                    {s.status === 'completed' ? 'Closed' : s.status.charAt(0).toUpperCase() + s.status.slice(1)}
                  </td>
                  <td className="border-t border-stroke">{new Date(s.updated_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
