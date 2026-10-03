import { useState } from 'react'
import type { User } from '../api'
import { api } from '../api'

export function Login({ onLogin }: { onLogin: (user: User) => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')

  return (
    <form
      className="surface-card mx-auto mt-24 flex w-80 flex-col gap-4 p-6"
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
      <input
        autoFocus
        value={username}
        onChange={e => setUsername(e.target.value)}
        placeholder="Username"
        className="rounded-control border border-stroke bg-raised px-3 py-2"
      />
      <input
        type="password"
        value={password}
        onChange={e => setPassword(e.target.value)}
        placeholder="Password"
        className="rounded-control border border-stroke bg-raised px-3 py-2"
      />
      {error && <p className="text-sm text-red-600">{error}</p>}
      <button className="rounded-control bg-accent py-2 font-semibold text-canvas">Sign in</button>
    </form>
  )
}
