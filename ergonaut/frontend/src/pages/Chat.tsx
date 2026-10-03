import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import type { AttachmentFile, Call, DelegatedRequest, Message, Pin, SessionDetail, Turn, Worker } from '../api'
import { api } from '../api'
import Files from '../components/Files'
import Markdown from '../components/Markdown'
import ModelPicker from '../components/ModelPicker'
import { PageViewer, Pins } from '../components/Pins'
import { AttachmentView, Transcript } from '../components/Transcript'

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

/** Whether a user message with this text was stored after ``line``. */
function stored(messages: Message[], text: string, line: number): boolean {
  const needle = JSON.stringify(text).slice(1, -1)
  return messages.some(m => m.line > line && m.role === 'user' && JSON.stringify(m.blocks).includes(needle))
}

export function Chat({ onChange }: { onChange: () => void }) {
  const { id = '' } = useParams()
  const [detail, setDetail] = useState<SessionDetail | null>(null)
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [last, setLast] = useState<Turn | null>(null)
  // Files picked or pasted for the next message, already uploaded to the session.
  type Outgoing = { id: string; filename: string; image: boolean }
  const [outgoing, setOutgoing] = useState<Outgoing[]>([])
  // Files sent with the queued message, shown with it until the worker stores it.
  const [sentFiles, setSentFiles] = useState<Outgoing[]>([])
  // The chat's files, so the transcript can show the ones a bot made where it made them.
  const [files, setFiles] = useState<AttachmentFile[]>([])
  const [uploading, setUploading] = useState(false)
  const picker = useRef<HTMLInputElement>(null)
  // A turn a worker is running: what was sent, and how the calls and messages looked then.
  const [pending, setPending] = useState<{ text: string; lastCall: string; line: number; approvals: string } | null>(
    null,
  )
  // Stop was pressed; the turn ends at its next step.
  const [stopping, setStopping] = useState(false)
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
  // The pinned page shown in place of the transcript, if any.
  const [openPin, setOpenPin] = useState<Pin | null>(null)
  // Bumped when the Files panel pins or unpins, so the strip reloads.
  const [pinsKey, setPinsKey] = useState(0)
  const bottom = useRef<HTMLDivElement>(null)
  const latest = useRef<SessionDetail | null>(null)
  latest.current = detail

  // Reloads fetch the newest page; older pages already loaded (See more) stay.
  const load = useCallback(async () => {
    const fresh = await api.session(id)
    setDetail(d => {
      const from = fresh.first_line
      if (!d || d.session.id !== fresh.session.id || d.first_line == null || from == null || d.first_line >= from)
        return fresh
      const older = { ...fresh, messages: d.messages.filter(m => m.line < from), calls: d.calls }
      return { ...merge(older, fresh.messages, fresh.calls), first_line: d.first_line, has_more: d.has_more }
    })
  }, [id])

  // Older messages of a long chat, a page at a time; the view stays where it was.
  const transcript = useRef<HTMLDivElement>(null)
  const keepScroll = useRef<number | null>(null)
  const skipScroll = useRef(false)
  const [loadingOlder, setLoadingOlder] = useState(false)
  async function loadOlder() {
    if (!detail?.has_more || detail.first_line == null || loadingOlder) return
    setLoadingOlder(true)
    const el = transcript.current
    try {
      const page = await api.session(id, detail.first_line)
      keepScroll.current = el ? el.scrollHeight - el.scrollTop : null
      setDetail(d =>
        d ? { ...merge(d, page.messages, page.calls), first_line: page.first_line, has_more: page.has_more } : d,
      )
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoadingOlder(false)
    }
  }
  useLayoutEffect(() => {
    const el = transcript.current
    if (keepScroll.current == null || !el) return
    el.scrollTop = el.scrollHeight - keepScroll.current
    keepScroll.current = null
    skipScroll.current = true
  }, [detail])

  useEffect(() => {
    setDetail(null)
    setLast(null)
    setOpenPin(null)
    setFiles([])
    load().catch(e => setError(String(e.message ?? e)))
  }, [load])

  // A bot's new file is saved while its tool runs; the tool's result message arrives right after.
  const messageCount = detail?.messages.length ?? 0
  useEffect(() => {
    if (!messageCount) return
    let current = true
    api
      .attachments(id)
      .then(list => current && setFiles(list))
      .catch(() => undefined)
    return () => {
      current = false
    }
  }, [id, messageCount])

  // A thread's generated title arrives after it starts; show it in the sidebar too.
  const title = detail?.session.title
  const firstTitle = useRef<string | undefined>(undefined)
  useEffect(() => {
    if (!title) return
    if (firstTitle.current !== undefined && firstTitle.current !== title) onChange()
    firstTitle.current = title
  }, [title, onChange])
  useEffect(() => {
    firstTitle.current = undefined
  }, [id])

  // ?pin=<url>&name=<name> (from the sidebar) opens that pin. Declared after the effect above,
  // which resets the viewer when the chat changes, so it runs second.
  const [search, setSearch] = useSearchParams()
  useEffect(() => {
    const url = search.get('pin')
    if (!url) return
    setOpenPin({ kind: 'file', name: search.get('name') || 'Pinned', url })
    setSearch({}, { replace: true })
  }, [search, setSearch])

  // Follow the session live, whichever process runs its turns.
  const loaded = detail !== null
  useEffect(() => {
    if (!loaded) return
    const messages = latest.current?.messages ?? []
    const after = messages.length ? messages[messages.length - 1].line : -1
    const events = new EventSource(`/api/sessions/${id}/events?after=${after}`)
    events.onmessage = e => {
      const { messages, calls, requests, title, workers } = JSON.parse(e.data) as {
        messages: Message[]
        calls: Call[]
        requests?: DelegatedRequest[]
        title?: string
        workers?: Worker[]
      }
      setDetail(d =>
        d
          ? {
              ...merge(d, messages, calls),
              ...(requests ? { requests } : {}),
              ...(title ? { session: { ...d.session, title } } : {}),
              ...(workers ? { workers } : {}),
            }
          : d,
      )
      if (requests) onChange()
    }
    return () => events.close()
  }, [id, loaded])

  useEffect(() => {
    if (skipScroll.current) {
      skipScroll.current = false
      return
    }
    bottom.current?.scrollIntoView({ behavior: 'smooth' })
  }, [detail, busy])

  // A queued turn is done once its call has finished: a new call for a
  // message (or the running call, once it took the message to steer it),
  // or the paused call moving on for an approval answer.
  useEffect(() => {
    if (!pending || !detail) return
    const call = detail.calls[detail.calls.length - 1]
    if (!call || call.status === 'in_progress') return
    const done = pending.text
      ? call.id !== pending.lastCall || stored(detail.messages, pending.text, pending.line)
      : call.status !== 'awaiting_approval' || JSON.stringify(call.pending_approvals ?? []) !== pending.approvals
    if (done) {
      setPending(null)
      setSentFiles([])
      onChange()
    }
  }, [detail, pending, onChange])

  // Stopping ends with the running call: it stopped, or a new call (an interrupt's) started.
  const lastCallId = detail?.calls[detail.calls.length - 1]?.id
  const lastStatus = detail?.calls[detail.calls.length - 1]?.status
  useEffect(() => {
    if (lastStatus !== 'in_progress') setStopping(false)
  }, [lastCallId, lastStatus])

  async function run(action: () => Promise<Turn>, sent = '') {
    setBusy(true)
    setError('')
    const calls = detail?.calls ?? []
    const messages = detail?.messages ?? []
    const before = {
      text: sent,
      lastCall: calls[calls.length - 1]?.id ?? '',
      line: messages.length ? messages[messages.length - 1].line : -1,
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

  // While a turn runs, a message steers it ("send") or stops it and starts the next one ("interrupt").
  function send(message: string, mode: 'send' | 'interrupt' = 'send') {
    if ((!message.trim() && !outgoing.length) || busy || uploading) return
    const ids = outgoing.map(f => f.id)
    setText('')
    setSentFiles(outgoing)
    setOutgoing([])
    if (mode === 'interrupt') setStopping(true)
    run(() => api.send(id, message, ids, mode), message || outgoing.map(f => f.filename).join(', '))
  }

  // Take back a message the running turn hasn't given the model yet; its text returns to the box.
  async function unsendMessage(itemId: string) {
    setError('')
    try {
      const item = await api.unsend(id, itemId)
      setText(current => (current.trim() ? `${item.text}\n\n${current}` : item.text))
      if (pending?.text === item.text) setPending(null)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      await load()
    }
  }

  async function stop() {
    setStopping(true)
    setError('')
    try {
      await api.stop(id)
    } catch (e) {
      setStopping(false)
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  async function attach(files: FileList | File[] | null) {
    if (!files || !files.length) return
    setUploading(true)
    setError('')
    try {
      for (const file of Array.from(files)) {
        const saved = await api.uploadAttachment(id, file)
        setOutgoing(list => [
          ...list,
          { id: saved.id, filename: saved.filename, image: saved.kind === 'image' || saved.view === 'image' },
        ])
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
  // A turn is running that Stop can reach (not just this browser's request in flight).
  const running = !!pending || lastCall?.status === 'in_progress'
  // The last turn failed (or hit its step limit) and the user hasn't resumed or dismissed it.
  const failedCall =
    !pending && lastCall && ['failed', 'turn_limited'].includes(lastCall.status) && !lastCall.dismissed
      ? lastCall
      : null
  // Show a queued message until the worker has stored it.
  const echo = pending?.text && !stored(detail.messages, pending.text, pending.line) ? pending.text : ''

  return (
    <div className="chat-session flex h-full">
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="chat-header flex flex-wrap items-center gap-3 px-6 py-4">
          <div>
            <div className="text-2xl font-bold">{detail.session.title}</div>
            <div className="mt-1 text-sm text-muted">
              {detail.session.bot} · {detail.session.role || 'session'} · {detail.messages.length} messages
              {detail.session.started_by && detail.session.started_by_id && (
                <>
                  {' · started by '}
                  <Link className="underline" to={`/s/${detail.session.started_by_id}`}>
                    {detail.session.started_by}
                  </Link>
                </>
              )}
            </div>
          </div>
          <div className="ml-auto">
            <ModelPicker
              bot={detail.session.bot}
              value={detail.session.model ?? ''}
              engineType={detail.session.engine_type}
              onPick={async model => {
                await api.setModel(id, model).catch(e => setError(String(e.message ?? e)))
                await load()
              }}
            />
          </div>
          {detail.session.role === 'thread' && (
            <button
              className="rounded-md border border-zinc-300 px-2 py-0.5 text-xs text-zinc-600 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-400"
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
            className={`rounded-md border px-2 py-0.5 text-xs ${showFiles ? 'border-indigo-400 text-indigo-700 dark:text-indigo-300' : 'border-zinc-300 text-zinc-600 dark:border-zinc-700 dark:text-zinc-400'}`}
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
        <Requests requests={detail.requests ?? []} />
        <Workers workers={detail.workers ?? []} />
        <Pins sessionId={id} refreshKey={`${detail.messages.length}:${pinsKey}`} open={openPin} onOpen={setOpenPin} />
        {openPin && <PageViewer pin={openPin} refreshKey={detail.messages.length} onClose={() => setOpenPin(null)} />}
        <div ref={transcript} className={`chat-transcript flex-1 overflow-y-auto px-6 py-5 ${openPin ? 'hidden' : ''}`}>
          {detail.has_more && (
            <div className="mb-4 flex justify-center">
              <button
                disabled={loadingOlder}
                className="rounded-full border border-stroke px-3 py-1 text-xs text-muted hover:bg-raised disabled:opacity-50"
                onClick={loadOlder}
              >
                {loadingOlder ? 'Loading…' : 'See more'}
              </button>
            </div>
          )}
          <Transcript messages={detail.messages} calls={detail.calls} files={files} complete={!detail.has_more} />
          {(detail.inbox ?? []).map(item => (
            <div key={item.id} className="mt-3 flex flex-col items-end">
              <div className="max-w-[80%] rounded-card border border-accent bg-indigo-tint px-4 py-3 text-ink">
                <Markdown text={item.text} />
                {item.files > 0 && <span className="text-xs opacity-80">📎 {item.files}</span>}
              </div>
              <div className="mt-1 flex items-center gap-2 text-xs text-zinc-500">
                <span>Waiting for the model's next step</span>
                {item.id && (
                  <button
                    className="rounded border border-zinc-300 px-1.5 hover:border-red-400 hover:text-red-600 dark:border-zinc-700"
                    title="Take this message back before the model sees it; its text goes back to the message box"
                    onClick={() => unsendMessage(item.id)}
                  >
                    Unsend
                  </button>
                )}
              </div>
            </div>
          ))}
          {echo && !(detail.inbox ?? []).some(item => item.text === echo) && (
            <div className="mt-3 flex flex-col items-end gap-1">
              {sentFiles.map(file => (
                <div key={file.id} className="max-w-[80%]">
                  <AttachmentView id={file.id} label={file.filename} image={file.image} />
                </div>
              ))}
              {(!sentFiles.length || echo !== sentFiles.map(f => f.filename).join(', ')) && (
                <div className="max-w-[80%] rounded-card bg-indigo-tint px-4 py-3 text-ink">
                  <Markdown text={echo} />
                </div>
              )}
            </div>
          )}
          {thinking && (
            <div className="mt-3 text-sm text-zinc-500">
              {stopping ? 'Stopping…' : pending && !lastCall?.status?.startsWith('in_') ? 'Queued…' : 'Thinking…'}
            </div>
          )}
          <div ref={bottom} />
        </div>
        {!!waiting.length && (
          <div className="mx-6 mb-3 rounded-card border border-warning/40 bg-amber-tint p-4 text-sm">
            <div className="mb-2 font-medium">Approve {waiting.map(a => a.name).join(', ')}?</div>
            <div className="flex gap-2">
              <button
                disabled={busy || !!pending}
                className="rounded-control bg-warning px-4 py-2 font-semibold text-canvas disabled:opacity-50"
                onClick={() =>
                  run(() =>
                    api.approve(
                      id,
                      true,
                      waiting.map(a => a.id),
                    ),
                  )
                }
              >
                Approve
              </button>
              <button
                disabled={busy || !!pending}
                className="rounded-control border border-warning/50 px-4 py-2 disabled:opacity-50"
                onClick={() =>
                  run(() =>
                    api.approve(
                      id,
                      false,
                      waiting.map(a => a.id),
                    ),
                  )
                }
              >
                Deny
              </button>
            </div>
          </div>
        )}
        {error && (
          <div className="mx-6 mb-2 rounded-card border border-danger/40 bg-red-tint p-3 text-sm text-danger">
            {error}
          </div>
        )}
        {failedCall && (
          <div className="mx-6 mb-2 rounded-card border border-danger/40 bg-red-tint px-4 py-3 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium text-red-700 dark:text-red-300">
                {failedCall.error_summary || 'The last turn failed.'}
              </span>
              {failedCall.error_hint && (
                <span className="text-zinc-600 dark:text-zinc-400">{failedCall.error_hint}</span>
              )}
              <span className="ml-auto flex gap-2">
                <button
                  disabled={busy}
                  className="rounded-control bg-accent px-3 py-1 text-xs font-semibold text-canvas disabled:opacity-50"
                  title="Continue this chat from where the last turn stopped"
                  onClick={() => run(() => api.resume(id), '')}
                >
                  Resume
                </button>
                <button
                  disabled={busy}
                  className="rounded-md border border-zinc-300 px-2.5 py-0.5 text-xs text-zinc-600 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-400"
                  title="Hide this error without resuming"
                  onClick={async () => {
                    await api.dismissCall(failedCall.id).catch(e => setError(String(e.message ?? e)))
                    await load()
                    onChange()
                  }}
                >
                  Dismiss
                </button>
              </span>
            </div>
            {failedCall.error && failedCall.error !== failedCall.error_summary && (
              <details className="mt-1 text-xs text-zinc-500">
                <summary className="cursor-pointer select-none">Details</summary>
                <pre className="mt-1 whitespace-pre-wrap break-words">{failedCall.error}</pre>
              </details>
            )}
          </div>
        )}
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
              <span
                key={file.id}
                className="flex items-center gap-1 rounded-full border border-zinc-300 px-2 py-0.5 text-xs dark:border-zinc-700"
              >
                {file.image ? <img src={api.viewUrl(file.id)} alt="" className="h-6 w-6 rounded object-cover" /> : '📎'}{' '}
                {file.filename}
                <button
                  type="button"
                  className="text-zinc-400 hover:text-red-600"
                  title="Remove"
                  onClick={() => unattach(file.id)}
                >
                  ✕
                </button>
              </span>
            ))}
          </div>
        )}
        <form
          className="chat-composer mx-4 mb-2 flex flex-wrap items-end gap-2 rounded-panel border border-stroke bg-surface p-4"
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
            className="rounded-control border border-stroke bg-raised px-3 py-2 text-lg disabled:opacity-50"
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
            placeholder={
              closed
                ? 'This session is closed'
                : waiting.length
                  ? 'Answer the approval first'
                  : archived
                    ? 'Archived: sending a message reopens it'
                    : running
                      ? 'Steer the bot: your message joins this turn'
                      : 'Message the bot'
            }
            className="min-w-48 flex-1 resize-none rounded-card border border-stroke bg-raised px-3 py-2 focus:outline-none"
          />
          {running && (
            <div className="flex flex-col gap-1">
              <button
                type="button"
                disabled={stopping}
                title="Stop the bot after the step it's on"
                className="flex-1 rounded-control border border-stroke bg-raised px-3 py-2 text-sm disabled:opacity-50"
                onClick={stop}
              >
                ⏹ Stop
              </button>
              {(!!text.trim() || !!outgoing.length) && (
                <button
                  type="button"
                  disabled={busy || uploading || stopping}
                  title="Stop the bot and answer this message instead"
                  className="flex-1 rounded-control bg-accent px-3 py-2 text-sm font-semibold text-canvas disabled:opacity-50"
                  onClick={() => send(text, 'interrupt')}
                >
                  Stop &amp; send
                </button>
              )}
            </div>
          )}
          <button
            disabled={busy || uploading || (!text.trim() && !outgoing.length)}
            title={running ? 'Send now; the bot sees it after the step it is on' : undefined}
            className="rounded-control bg-accent px-5 py-2.5 font-semibold text-canvas disabled:opacity-50"
          >
            Send
          </button>
        </form>
      </div>
      {showFiles && (
        <Files
          sessionId={id}
          refreshKey={detail.messages.length}
          onPinsChanged={() => setPinsKey(k => k + 1)}
          onView={setOpenPin}
        />
      )}
    </div>
  )
}

const REQUEST_STATUS: Record<DelegatedRequest['status'], string> = {
  queued: 'queued',
  delivered: 'working on it',
  waiting: 'waiting for your approval',
  answered: 'answered',
  failed: 'failed',
}

// Delegated work still open: what this chat is waiting on, and what it's doing for others.
const WORKER_ICON: Record<Worker['status'], string> = {
  queued: '…',
  running: '',
  completed: '✓',
  failed: '✗',
  cancelled: '⊘',
}

// Long-running work this chat started: running ones with their latest progress, and the
// last few that finished (their results also arrive as messages).
function Workers({ workers }: { workers: Worker[] }) {
  const [showDone, setShowDone] = useState(false)
  const running = workers.filter(w => w.status === 'queued' || w.status === 'running')
  const done = workers.filter(w => !running.includes(w))
  if (!workers.length) return null
  return (
    <div className="mx-6 mb-2 flex flex-col gap-2 rounded-card border border-stroke bg-surface px-4 py-3 text-xs">
      {running.map(w => (
        <div key={w.id} className="flex items-center gap-2 truncate">
          {w.status === 'running' ? (
            <span className="h-2.5 w-2.5 shrink-0 animate-spin rounded-full border-[1.5px] border-indigo-500 border-t-transparent" />
          ) : (
            <span className="text-zinc-400">{WORKER_ICON[w.status]}</span>
          )}
          <span className="font-medium">{w.title}</span>
          <span className="truncate text-zinc-500">{w.progress || w.status}</span>
        </div>
      ))}
      {!!done.length && (
        <button className="self-start text-zinc-400 hover:text-zinc-600" onClick={() => setShowDone(s => !s)}>
          {showDone ? '▾' : '▸'} {done.length} finished worker{done.length === 1 ? '' : 's'}
        </button>
      )}
      {showDone &&
        done.map(w => (
          <div key={w.id} className="flex items-center gap-2 truncate" title={w.error || undefined}>
            <span className={w.status === 'completed' ? 'text-emerald-600' : 'text-red-600'}>
              {WORKER_ICON[w.status]}
            </span>
            <span>{w.title}</span>
            <span className="truncate text-zinc-400">{w.error || w.progress}</span>
          </div>
        ))}
    </div>
  )
}

function Requests({ requests }: { requests: DelegatedRequest[] }) {
  const open = requests.filter(r => r.status !== 'answered' && r.status !== 'failed')
  if (!open.length) return null
  return (
    <div className="mx-6 mb-2 flex flex-col gap-2 rounded-card border border-teal/40 bg-teal-tint px-4 py-3 text-xs">
      {open.map(r => (
        <div key={r.id} className="flex items-center gap-2 truncate">
          <span className={r.status === 'waiting' ? 'text-amber-600' : 'text-teal-600'}>
            {r.direction === 'out' ? '⏳' : '📥'}
          </span>
          <span className="text-zinc-500">{r.direction === 'out' ? 'Waiting on' : 'Working for'}</span>
          {r.other_session_id ? (
            <Link
              to={`/s/${r.other_session_id}`}
              className="font-medium text-indigo-600 hover:underline dark:text-indigo-400"
            >
              {r.other}
            </Link>
          ) : (
            <span className="font-medium">{r.other}</span>
          )}
          <span className="text-zinc-500">· {REQUEST_STATUS[r.status]}</span>
          <span className="truncate text-zinc-400">“{r.text}”</span>
        </div>
      ))}
    </div>
  )
}
