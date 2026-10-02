import { useCallback, useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import type { Call, Message, SessionDetail, Turn } from '../api'
import { api } from '../api'
import Files from '../components/Files'
import { Transcript } from '../components/Transcript'

/** Fold a live update into the transcript: messages replace by line, calls by id. */
function merge(detail: SessionDetail, messages: Message[], calls: Call[]): SessionDetail {
  const byLine = new Map(detail.messages.map(m => [m.line, m]))
  for (const m of messages) byLine.set(m.line, m)
  const byId = new Map(detail.calls.map(c => [c.id, c]))
  for (const c of calls) byId.set(c.id, c)
  return {
    ...detail,
    messages: [...byLine.values()].sort((a, b) => a.line - b.line),
    calls: [...byId.values()].sort((a, b) => a.created_at.localeCompare(b.created_at)),
  }
}

export function Chat({ onChange }: { onChange: () => void }) {
  const { id = '' } = useParams()
  const [detail, setDetail] = useState<SessionDetail | null>(null)
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [last, setLast] = useState<Turn | null>(null)
  // Files picked or pasted for the next message, already uploaded to the session.
  const [outgoing, setOutgoing] = useState<{ id: string; filename: string }[]>([])
  const [uploading, setUploading] = useState(false)
  const picker = useRef<HTMLInputElement>(null)
  // A turn a worker is running: what was sent and how the calls looked then.
  const [pending, setPending] = useState<{ text: string; calls: number; approvals: string } | null>(null)
  // The Files panel stays open or closed across sessions, per browser.
  const [showFiles, setShowFiles] = useState(() => {
    try {
      return localStorage.getItem('ergonaut.files') === 'open'
    } catch {
      return false
    }
  })
  function toggleFiles(open: boolean) {
    setShowFiles(open)
    try {
      localStorage.setItem('ergonaut.files', open ? 'open' : 'closed')
    } catch {
      // storage unavailable
    }
  }
  const bottom = useRef<HTMLDivElement>(null)
  const latest = useRef<SessionDetail | null>(null)
  latest.current = detail

  const load = useCallback(async () => setDetail(await api.session(id)), [id])

  useEffect(() => {
    setDetail(null)
    setLast(null)
    load().catch(e => setError(String(e.message ?? e)))
  }, [load])

  // Follow the session live, whichever process runs its turns.
  const loaded = detail !== null
  useEffect(() => {
    if (!loaded) return
    const messages = latest.current?.messages ?? []
    const after = messages.length ? messages[messages.length - 1].line : -1
    const events = new EventSource(`/api/sessions/${id}/events?after=${after}`)
    events.onmessage = e => {
      const { messages, calls } = JSON.parse(e.data) as { messages: Message[]; calls: Call[] }
      setDetail(d => (d ? merge(d, messages, calls) : d))
    }
    return () => events.close()
  }, [id, loaded])

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: 'smooth' })
  }, [detail, busy])

  // A queued turn is done once its call has finished: a new call for a
  // message, or the paused call moving on for an approval answer.
  useEffect(() => {
    if (!pending || !detail) return
    const call = detail.calls[detail.calls.length - 1]
    if (!call || call.status === 'in_progress') return
    const done = pending.text
      ? detail.calls.length > pending.calls
      : call.status !== 'awaiting_approval' || JSON.stringify(call.pending_approvals ?? []) !== pending.approvals
    if (done) {
      setPending(null)
      onChange()
    }
  }, [detail, pending, onChange])

  async function run(action: () => Promise<Turn>, sent = '') {
    setBusy(true)
    setError('')
    const calls = detail?.calls ?? []
    const before = {
      text: sent,
      calls: calls.length,
      approvals: JSON.stringify(calls[calls.length - 1]?.pending_approvals ?? []),
    }
    try {
      const turn = await action()
      setLast(turn)
      if (turn.queued) {
        setPending(before)
      } else {
        if (turn.error) setError(turn.error)
        await load()
        onChange()
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  function send(message: string) {
    if ((!message.trim() && !outgoing.length) || busy || pending || uploading) return
    const ids = outgoing.map(f => f.id)
    setText('')
    setOutgoing([])
    run(() => api.send(id, message, ids), message || outgoing.map(f => f.filename).join(', '))
  }

  async function attach(files: FileList | File[] | null) {
    if (!files || !files.length) return
    setUploading(true)
    setError('')
    try {
      for (const file of Array.from(files)) {
        const saved = await api.uploadAttachment(id, file)
        setOutgoing(list => [...list, { id: saved.id, filename: saved.filename }])
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setUploading(false)
      if (picker.current) picker.current.value = ''
    }
  }

  async function unattach(fileId: string) {
    setOutgoing(list => list.filter(f => f.id !== fileId))
    await api.deleteAttachment(fileId).catch(() => undefined)
  }

  if (!detail) return <div className="p-6 text-zinc-500">{error || 'Loading…'}</div>

  const lastCall = detail.calls[detail.calls.length - 1]
  const waiting = lastCall?.status === 'awaiting_approval' ? lastCall.pending_approvals : []
  const suggestions = !waiting.length ? (last?.suggestions ?? lastCall?.response?.suggestions ?? []) : []
  const archived = detail.session.status === 'completed' && detail.session.role === 'thread'
  const closed = detail.session.status === 'completed' && !archived
  const thinking = busy || !!pending || lastCall?.status === 'in_progress'
  const callError = !pending && lastCall?.status === 'failed' ? lastCall.error : ''
  // Show a queued message until the worker has stored it.
  const echo = pending?.text && !detail.messages.some(m => m.role === 'user' && JSON.stringify(m.blocks).includes(JSON.stringify(pending.text).slice(1, -1))) ? pending.text : ''

  return (
    <div className="flex h-full">
    <div className="flex min-w-0 flex-1 flex-col">
      <header className="flex items-center gap-3 border-b border-zinc-200 px-6 py-3 dark:border-zinc-800">
        <div>
          <div className="font-semibold">{detail.session.title}</div>
          <div className="text-xs text-zinc-500">
            {detail.session.bot} · {detail.session.role || 'session'} · {detail.messages.length} messages
          </div>
        </div>
        {detail.session.role === 'thread' && (
          <button
            className="ml-auto rounded-md border border-zinc-300 px-2 py-0.5 text-xs text-zinc-600 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-400"
            disabled={archived || busy}
            title={archived ? 'Archived; a new message reopens it' : 'Archive this thread'}
            onClick={async () => {
              await api.close(id).catch(e => setError(String(e.message ?? e)))
              await load()
              onChange()
            }}
          >
            {archived ? 'Archived' : 'Archive'}
          </button>
        )}
        <button
          className={`${detail.session.role === 'thread' ? '' : 'ml-auto '}rounded-md border px-2 py-0.5 text-xs ${showFiles ? 'border-indigo-400 text-indigo-700 dark:text-indigo-300' : 'border-zinc-300 text-zinc-600 dark:border-zinc-700 dark:text-zinc-400'}`}
          onClick={() => toggleFiles(!showFiles)}
        >
          📎 Files
        </button>
        <a
          className="text-xs text-zinc-500 underline"
          href={`data:application/json,${encodeURIComponent(JSON.stringify(detail, null, 2))}`}
          download={`session-${detail.session.id}.json`}
        >
          Export JSON
        </a>
      </header>
      <div className="flex-1 overflow-y-auto px-6 py-4">
        <Transcript messages={detail.messages} calls={detail.calls} />
        {echo && (
          <div className="mt-3 flex justify-end">
            <div className="max-w-[80%] rounded-2xl bg-indigo-600/70 px-4 py-2 whitespace-pre-wrap text-white">{echo}</div>
          </div>
        )}
        {thinking && <div className="mt-3 text-sm text-zinc-500">{pending && !lastCall?.status?.startsWith('in_') ? 'Queued…' : 'Thinking…'}</div>}
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
      {(error || callError) && <div className="mx-6 mb-2 text-sm text-red-600">{error || callError}</div>}
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
      {!!outgoing.length && (
        <div className="mx-4 flex flex-wrap gap-2 pt-2">
          {outgoing.map(file => (
            <span key={file.id} className="flex items-center gap-1 rounded-full border border-zinc-300 px-2 py-0.5 text-xs dark:border-zinc-700">
              📎 {file.filename}
              <button type="button" className="text-zinc-400 hover:text-red-600" title="Remove" onClick={() => unattach(file.id)}>
                ✕
              </button>
            </span>
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
        <input ref={picker} type="file" multiple hidden onChange={e => attach(e.target.files)} />
        <button
          type="button"
          disabled={closed || !!waiting.length || uploading}
          title="Attach images, PDFs or other files"
          className="rounded-lg border border-zinc-300 px-3 text-lg disabled:opacity-50 dark:border-zinc-700"
          onClick={() => picker.current?.click()}
        >
          {uploading ? '…' : '📎'}
        </button>
        <textarea
          onPaste={e => {
            const files = Array.from(e.clipboardData.files)
            if (files.length) {
              e.preventDefault()
              attach(files)
            }
          }}
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
          placeholder={closed ? 'This session is closed' : waiting.length ? 'Answer the approval first' : archived ? 'Archived: sending a message reopens it' : 'Message the bot'}
          className="flex-1 resize-none rounded-lg border border-zinc-300 bg-transparent px-3 py-2 focus:border-indigo-500 focus:outline-none dark:border-zinc-700"
        />
        <button disabled={busy || uploading || (!text.trim() && !outgoing.length)} className="rounded-lg bg-indigo-600 px-4 text-white disabled:opacity-50">
          Send
        </button>
      </form>
    </div>
    {showFiles && <Files sessionId={id} refreshKey={detail.messages.length} />}
    </div>
  )
}
