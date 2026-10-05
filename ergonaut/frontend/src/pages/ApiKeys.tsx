import { useEffect, useState } from 'react'
import type { ApiKey } from '../api'
import { ApiError, api } from '../api'
import { ago } from '../time'

// Keys for scripts and agents (the Ergo client skill): Authorization: Bearer ergo_...
export function ApiKeysPage() {
  const [keys, setKeys] = useState<ApiKey[] | null>(null)
  const [name, setName] = useState('')
  const [made, setMade] = useState<{ name: string; key: string } | null>(null)
  const [error, setError] = useState('')

  const load = () =>
    api
      .apiKeys()
      .then(setKeys)
      .catch(() => setKeys([]))
  useEffect(() => {
    load()
  }, [])

  const create = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    try {
      const row = await api.createApiKey(name)
      setMade({ name: row.name, key: row.key })
      setName('')
      load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not make the key')
    }
  }

  const revoke = async (key: ApiKey) => {
    if (!window.confirm(`Revoke ${key.name}? Anything using it stops working.`)) return
    await api.revokeApiKey(key.id)
    load()
  }

  const field =
    'rounded-card border border-stroke bg-raised px-4 py-3 text-sm text-ink focus-visible:outline-2 focus-visible:outline-accent'

  return (
    <div className="page-content h-full overflow-y-auto">
      <h1 className="page-title">API keys</h1>
      <p className="page-lede mt-3">
        A key lets a script or an agent (Claude Code or Codex with the Ergo client skill) use this server as you. Send
        it as <code>Authorization: Bearer ergo_…</code>.
      </p>
      <form onSubmit={create} className="mb-6 mt-6 flex flex-wrap gap-4">
        <input
          value={name}
          onChange={e => setName(e.target.value)}
          placeholder="Where it's used, e.g. rigel-claude"
          aria-label="Key name"
          className={`${field} min-w-60 flex-1`}
        />
        <button
          type="submit"
          disabled={!name.trim()}
          className="rounded-control border border-accent px-4 py-2 text-sm font-semibold text-accent hover:bg-indigo-tint disabled:opacity-50"
        >
          Make key
        </button>
      </form>
      {error && <p className="mb-4 text-sm text-danger">{error}</p>}
      {made && (
        <div className="surface-card mb-6 p-4">
          <p className="text-sm font-semibold">Copy the key for {made.name} now. It won't be shown again.</p>
          <code className="mt-2 block break-all rounded-control bg-raised p-3 text-sm">{made.key}</code>
        </div>
      )}
      {keys === null ? (
        <p className="text-zinc-500">Loading…</p>
      ) : !keys.length ? (
        <p className="text-zinc-500">No keys yet.</p>
      ) : (
        <div className="data-scroll rounded-card border border-stroke">
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-zinc-500">
              <tr>
                <th>Name</th>
                <th>Key</th>
                <th>Made</th>
                <th>Last used</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {keys.map(k => (
                <tr key={k.id} className="border-t border-zinc-200 dark:border-zinc-800">
                  <td>{k.name}</td>
                  <td>
                    <code>{k.hint}…</code>
                  </td>
                  <td>{ago(k.created_at)}</td>
                  <td>{k.last_used_at ? ago(k.last_used_at) : 'Never'}</td>
                  <td className="text-right">
                    <button type="button" onClick={() => revoke(k)} className="text-sm text-danger hover:underline">
                      Revoke
                    </button>
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
