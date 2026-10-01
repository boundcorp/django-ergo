import { useState } from 'react'
import type { User } from '../api'
import { api } from '../api'

export function Login({ onLogin }: { onLogin: (user: User) => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')

  return (
    <form
      className="mx-auto mt-24 flex w-80 flex-col gap-3"
      onSubmit={async e => {
        e.preventDefault()
        try {
          onLogin(await api.login(username, password))
        } catch (err) {
          setError(err instanceof Error ? err.message : String(err))
        }
      }}
    >
      <h1 className="text-xl font-semibold">Sign in to Ergonaut</h1>
      <input autoFocus value={username} onChange={e => setUsername(e.target.value)} placeholder="Username" className="rounded-lg border border-zinc-300 bg-transparent px-3 py-2 dark:border-zinc-700" />
      <input type="password" value={password} onChange={e => setPassword(e.target.value)} placeholder="Password" className="rounded-lg border border-zinc-300 bg-transparent px-3 py-2 dark:border-zinc-700" />
      {error && <p className="text-sm text-red-600">{error}</p>}
      <button className="rounded-lg bg-indigo-600 py-2 text-white">Sign in</button>
    </form>
  )
}
