import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'

export function DebugShortcut({ sessionId }: { sessionId: string }) {
  const navigate = useNavigate()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function open() {
    if (busy) return
    setBusy(true)
    setError('')
    try {
      const { url } = await api.debugShortcut(sessionId)
      if (!/^\/s\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(url)) {
        throw new Error('Invalid chat link returned by the server')
      }
      navigate('/bots/devbox/new-thread', { state: { debugSourceUrl: new URL(url, window.location.origin).href } })
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not open debug thread')
      setBusy(false)
    }
  }

  return (
    <>
      {error && (
        <span role="alert" className="self-center text-xs text-danger">
          {error}
        </span>
      )}
      <button
        type="button"
        disabled={busy}
        onClick={open}
        title="Start a devbox thread with a link to this chat"
        className="self-end rounded-full border border-indigo-400 bg-surface px-3 py-1.5 text-xs font-semibold text-indigo-700 shadow-sm hover:bg-indigo-tint disabled:opacity-50 dark:text-indigo-300"
      >
        {busy ? 'Opening…' : 'Debug'}
      </button>
    </>
  )
}
