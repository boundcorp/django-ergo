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

  return (
    <div className="mx-auto max-w-4xl p-6">
      <h1 className="mb-4 text-xl font-semibold">Sessions</h1>
      <div className="mb-4 flex flex-wrap gap-2">
        <input
          value={q}
          onChange={e => setQ(e.target.value)}
          placeholder="Search message text"
          className="min-w-60 flex-1 rounded-lg border border-zinc-300 bg-transparent px-3 py-2 dark:border-zinc-700"
        />
        <select value={bot} onChange={e => setBot(e.target.value)} className="rounded-lg border border-zinc-300 bg-transparent px-2 dark:border-zinc-700">
          <option value="">All bots</option>
          {bots.map(b => (
            <option key={b.name}>{b.name}</option>
          ))}
        </select>
        <select value={status} onChange={e => setStatus(e.target.value)} className="rounded-lg border border-zinc-300 bg-transparent px-2 dark:border-zinc-700">
          <option value="">Any status</option>
          <option value="active">Active</option>
          <option value="completed">Closed</option>
        </select>
      </div>
      {sessions === null ? (
        <p className="text-zinc-500">Loading…</p>
      ) : !sessions.length ? (
        <p className="text-zinc-500">No sessions match.</p>
      ) : (
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-zinc-500">
            <tr>
              <th className="py-2">Session</th>
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
                <td className="py-2">
                  <Link className="text-indigo-600 hover:underline" to={`/s/${s.id}`}>
                    {s.title}
                  </Link>
                </td>
                <td>{s.bot}</td>
                <td>{s.role || '-'}</td>
                <td>{s.username}</td>
                <td>{s.status}</td>
                <td>{new Date(s.updated_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
