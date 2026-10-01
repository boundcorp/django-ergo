import { useCallback, useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import type { SessionDetail, Turn } from '../api'
import { api } from '../api'
import { Transcript } from '../components/Transcript'

export function Chat({ onChange }: { onChange: () => void }) {
  const { id = '' } = useParams()
  const [detail, setDetail] = useState<SessionDetail | null>(null)
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [last, setLast] = useState<Turn | null>(null)
  const bottom = useRef<HTMLDivElement>(null)

  const load = useCallback(async () => setDetail(await api.session(id)), [id])

  useEffect(() => {
    setDetail(null)
    setLast(null)
    load().catch(e => setError(String(e.message ?? e)))
  }, [load])

  useEffect(() => bottom.current?.scrollIntoView({ behavior: 'smooth' }), [detail, busy])

  async function run(action: () => Promise<Turn>) {
    setBusy(true)
    setError('')
    try {
      const turn = await action()
      setLast(turn)
      if (turn.error) setError(turn.error)
      await load()
      onChange()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  function send(message: string) {
    if (!message.trim() || busy) return
    setText('')
    run(() => api.send(id, message))
  }

  if (!detail) return <div className="p-6 text-zinc-500">{error || 'Loading…'}</div>

  const lastCall = detail.calls[detail.calls.length - 1]
  const waiting = lastCall?.status === 'awaiting_approval' ? lastCall.pending_approvals : []
  const suggestions = !waiting.length ? (last?.suggestions ?? lastCall?.response?.suggestions ?? []) : []
  const closed = detail.session.status === 'completed'

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-3 border-b border-zinc-200 px-6 py-3 dark:border-zinc-800">
        <div>
          <div className="font-semibold">{detail.session.title}</div>
          <div className="text-xs text-zinc-500">
            {detail.session.bot} · {detail.session.role || 'session'} · {detail.messages.length} messages
          </div>
        </div>
        <a
          className="ml-auto text-xs text-zinc-500 underline"
          href={`data:application/json,${encodeURIComponent(JSON.stringify(detail, null, 2))}`}
          download={`session-${detail.session.id}.json`}
        >
          Export JSON
        </a>
      </header>
      <div className="flex-1 overflow-y-auto px-6 py-4">
        <Transcript messages={detail.messages} calls={detail.calls} />
        {busy && <div className="mt-3 text-sm text-zinc-500">Thinking…</div>}
        <div ref={bottom} />
      </div>
      {!!waiting.length && (
        <div className="mx-6 mb-2 rounded-lg border border-amber-400/60 bg-amber-50 p-3 text-sm dark:bg-amber-950/30">
          <div className="mb-2 font-medium">
            Approve {waiting.map(a => a.name).join(', ')}?
          </div>
          <div className="flex gap-2">
            <button disabled={busy} className="rounded-md bg-emerald-600 px-3 py-1 text-white disabled:opacity-50" onClick={() => run(() => api.approve(id, true))}>
              Approve
            </button>
            <button disabled={busy} className="rounded-md border border-zinc-300 px-3 py-1 disabled:opacity-50 dark:border-zinc-700" onClick={() => run(() => api.approve(id, false))}>
              Deny
            </button>
          </div>
        </div>
      )}
      {error && <div className="mx-6 mb-2 text-sm text-red-600">{error}</div>}
      {!!suggestions.length && (
        <div className="mx-6 mb-2 flex flex-wrap gap-2">
          {suggestions.map(s => (
            <button
              key={s}
              disabled={busy}
              className="rounded-full border border-indigo-300 px-3 py-1 text-sm text-indigo-700 hover:bg-indigo-50 disabled:opacity-50 dark:border-indigo-700 dark:text-indigo-300 dark:hover:bg-indigo-950"
              onClick={() => send(s)}
            >
              {s}
            </button>
          ))}
        </div>
      )}
      <form
        className="flex gap-2 border-t border-zinc-200 p-4 dark:border-zinc-800"
        onSubmit={e => {
          e.preventDefault()
          send(text)
        }}
      >
        <textarea
          value={text}
          disabled={closed || !!waiting.length}
          onChange={e => setText(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              send(text)
            }
          }}
          rows={2}
          placeholder={closed ? 'This session is closed' : waiting.length ? 'Answer the approval first' : 'Message the bot'}
          className="flex-1 resize-none rounded-lg border border-zinc-300 bg-transparent px-3 py-2 focus:border-indigo-500 focus:outline-none dark:border-zinc-700"
        />
        <button disabled={busy || !text.trim()} className="rounded-lg bg-indigo-600 px-4 text-white disabled:opacity-50">
          Send
        </button>
      </form>
    </div>
  )
}
