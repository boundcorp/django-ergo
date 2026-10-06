import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import type {
  AttachmentFile,
  Call,
  DelegatedRequest,
  Message,
  Pin,
  PrLink,
  SentCard,
  SessionDetail,
  Turn,
  Worker,
} from '../api'
import { api } from '../api'
import { useDraft } from '../draft'
import { prepareChatSend, type ChatSendSource } from '../chatSend'
import { suggestionsFromMessages, statusSummary } from '../chatLayout'
import Files from '../components/Files'
import Markdown from '../components/Markdown'
import ModelPicker, { modelLabel, useBotModels } from '../components/ModelPicker'
import { PageViewer, Pins } from '../components/Pins'
import { AttachmentView, replySuggestions, Transcript } from '../components/Transcript'
import { ModelLink, MobileSessionBar, SuggestionChips, ThreadOptions } from '../components/ChatChrome'
import { WorkerActivityView, WorkerPulse } from '../components/WorkerActivity'
import { agoLong } from '../time'

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

/** Download the session as JSON. Built on click: a data: link of a long chat runs to megabytes,
 * re-serialized on every render, and mobile Safari drops the page under that weight. */
function exportJson(detail: SessionDetail) {
  const blob = new Blob([JSON.stringify(detail, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `session-${detail.session.id}.json`
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

/** Whether a user message with this text was stored after ``line``. */
function stored(messages: Message[], text: string, line: number): boolean {
  const needle = JSON.stringify(text).slice(1, -1)
  return messages.some(m => m.line > line && m.role === 'user' && JSON.stringify(m.blocks).includes(needle))
}

/** The selected pin replaces the transcript while keeping the composer available. */
export function PinnedDashboard({
  pin,
  refreshKey,
  onClose,
}: {
  pin: Pin | null
  refreshKey: unknown
  onClose: () => void
}) {
  return pin ? <PageViewer pin={pin} refreshKey={refreshKey} onClose={onClose} /> : null
}

export function Chat({ onChange }: { onChange: () => void }) {
  const { id = '' } = useParams()
  const [detail, setDetail] = useState<SessionDetail | null>(null)
  const [text, setText] = useDraft(id)
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
  const [pinCount, setPinCount] = useState(0)
  const onPinCount = useCallback((count: number) => setPinCount(count), [])
  // The thread options: a sheet on a phone, a dropdown in the header otherwise. The composer's
  // model link opens whichever one this screen has.
  const [sheetOpen, setSheetOpen] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const models = useBotModels(detail?.session.bot ?? '')
  function openOptions() {
    if (window.matchMedia('(max-width: 640px)').matches) setSheetOpen(true)
    else setMenuOpen(true)
  }
  // Same call whichever picker (header dropdown or phone sheet) chose the model.
  async function pickModel(model: string) {
    await api.setModel(id, model).catch(e => setError(String(e.message ?? e)))
    await load()
  }
  const composer = useRef<HTMLTextAreaElement>(null)
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
  // The view follows new messages only while it's at the bottom; scrolled up, it stays put.
  const atBottom = useRef(true)
  const [scrolledUp, setScrolledUp] = useState(false)
  const [unseen, setUnseen] = useState(false)
  const seenCount = useRef(0)
  function onTranscriptScroll() {
    const el = transcript.current
    if (!el) return
    atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
    setScrolledUp(!atBottom.current)
    if (atBottom.current) setUnseen(false)
    if (window.matchMedia('(max-width: 640px)').matches) {
      const next = el.scrollTop > 32 ? '1' : ''
      if (document.documentElement.dataset.chatScrolled !== next) document.documentElement.dataset.chatScrolled = next
    }
  }
  // Scrolls the message list itself. scrollIntoView would also scroll every ancestor, including the
  // page, which is how the composer used to end up at the top of the screen with the chat above it.
  function pinToBottom(behavior: ScrollBehavior = 'auto') {
    const el = transcript.current
    if (el) el.scrollTo({ top: el.scrollHeight, behavior })
  }
  function toBottom(behavior: ScrollBehavior = 'auto') {
    atBottom.current = true
    setScrolledUp(false)
    setUnseen(false)
    pinToBottom(behavior)
  }
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
    atBottom.current = true
    setScrolledUp(false)
    setUnseen(false)
    setOpenPin(null)
    setFiles([])
    load().catch(e => setError(String(e.message ?? e)))
  }, [load])

  // A bot's new file is saved while its tool runs; the tool's result message arrives right after.
  // Pull requests are recorded when the turn ends, just after its reply: refetch when they change too.
  const messageCount = detail?.messages.length ?? 0
  const prKey = (detail?.prs ?? []).map(p => p.url).join()
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
  }, [id, messageCount, prKey])

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
    setPinCount(0)
    delete document.documentElement.dataset.chatScrolled
  }, [id])
  useEffect(() => {
    return () => {
      delete document.documentElement.dataset.chatScrolled
    }
  }, [])
  useLayoutEffect(() => {
    const el = composer.current
    if (!el) return
    const mobile = window.matchMedia('(max-width: 640px)').matches
    el.rows = mobile ? 1 : 2
    if (!mobile) {
      el.style.height = ''
      return
    }
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 128)}px`
  }, [text, detail])

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
      const { messages, calls, requests, title, workers, sent, prs } = JSON.parse(e.data) as {
        messages: Message[]
        calls: Call[]
        requests?: DelegatedRequest[]
        title?: string
        workers?: Worker[]
        sent?: SentCard[]
        prs?: PrLink[]
      }
      setDetail(d =>
        d
          ? {
              ...merge(d, messages, calls),
              // New lines past the newest one count toward the session's total.
              message_count:
                d.message_count == null
                  ? undefined
                  : d.message_count +
                    new Set(messages.map(m => m.line).filter(line => !d.messages.some(m => m.line === line))).size,
              ...(requests ? { requests } : {}),
              ...(title ? { session: { ...d.session, title } } : {}),
              ...(workers ? { workers } : {}),
              ...(sent ? { sent } : {}),
              ...(prs ? { prs } : {}),
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
    if (atBottom.current) pinToBottom()
    else if ((detail?.messages.length ?? 0) > seenCount.current) setUnseen(true)
    seenCount.current = detail?.messages.length ?? 0
  }, [detail, busy])

  // The list keeps its place at the bottom when its own height changes (the phone keyboard opening,
  // the composer growing, the options sheet closing), but only if the reader was already there.
  useEffect(() => {
    const el = transcript.current
    if (!el) return
    const observer = new ResizeObserver(() => {
      if (atBottom.current) el.scrollTop = el.scrollHeight
    })
    observer.observe(el)
    return () => observer.disconnect()
  }, [loaded])

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
  function send(message: string, mode: 'send' | 'interrupt' = 'send', source: ChatSendSource = 'composer') {
    const plan = prepareChatSend(source, message, text, outgoing)
    if ((!plan.message.trim() && !plan.attachmentIds.length) || busy || uploading) return
    if (plan.composer === 'clear') {
      setText(plan.draft)
      setSentFiles(plan.sentFiles)
      setOutgoing(plan.outgoing)
    }
    if (mode === 'interrupt') setStopping(true)
    toBottom()
    run(
      () => api.send(id, plan.message, plan.attachmentIds, mode),
      plan.message || plan.sentFiles.map(f => f.filename).join(', '),
    )
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
  const turnSuggestions = !waiting.length ? replySuggestions(last?.suggestions ?? lastCall?.response?.suggestions) : []
  const mobileSuggestions = turnSuggestions.length
    ? turnSuggestions
    : replySuggestions(suggestionsFromMessages(detail.messages))
  const summary = statusSummary(detail.requests ?? [], detail.workers ?? [], pinCount)
  const archived = detail.session.status === 'completed' && detail.session.role === 'thread'
  // A turn doesn't touch the session row, so its updated_at stays at creation.
  const lastActivity = [detail.session.updated_at, detail.messages[detail.messages.length - 1]?.timestamp]
    .filter((t): t is string => !!t)
    .reduce((a, b) => (Date.parse(b) > Date.parse(a) ? b : a))
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
        <div className="chat-desktop-chrome">
          <header className="chat-header flex flex-wrap items-center gap-3 px-4 py-4 sm:px-6">
            <div className="min-w-0">
              <div className="chat-title-bot truncate text-xs font-semibold uppercase tracking-wide text-muted">
                {detail.session.bot}
              </div>
              <h1 className="chat-title font-display text-2xl font-bold [overflow-wrap:anywhere]">
                {detail.session.title}
              </h1>
              <div className="mt-1 text-sm text-muted">
                {detail.session.role || 'session'} · {(detail.message_count ?? detail.messages.length).toLocaleString()}{' '}
                messages · Updated {agoLong(lastActivity)} · Session{' '}
                {archived ? 'archived' : closed ? 'closed' : 'active'}
                {detail.session.started_by && detail.session.started_by_id && (
                  <>
                    {' · started by '}
                    <Link className="underline" to={`/s/${detail.session.started_by_id}`}>
                      {detail.session.started_by}
                    </Link>
                  </>
                )}
              </div>
              {archived && detail.session.resolved_summary && (
                <div className="mt-1 text-sm text-success">
                  ✓ Resolved{detail.session.resolved_by ? ` by ${detail.session.resolved_by}` : ''}:{' '}
                  <span className="text-ink">{detail.session.resolved_summary}</span>
                </div>
              )}
            </div>
            <div className="ml-auto flex flex-wrap items-center gap-3">
              {models && (
                <ThreadOptions open={menuOpen} onOpenChange={setMenuOpen}>
                  <ModelPicker models={models} value={detail.session.model ?? ''} onPick={pickModel} large />
                </ThreadOptions>
              )}
              {detail.session.role === 'thread' && (
                <button
                  className="rounded-md border border-zinc-300 px-2 py-0.5 text-xs text-zinc-600 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-400"
                  disabled={archived || busy}
                  title={archived ? 'Resolved; a new message reopens it' : 'Mark this thread resolved'}
                  onClick={async () => {
                    await api.close(id).catch(e => setError(String(e.message ?? e)))
                    await load()
                    onChange()
                  }}
                >
                  {archived ? 'Resolved' : 'Resolve'}
                </button>
              )}
              <button
                className={`rounded-md border px-2 py-0.5 text-xs ${showFiles ? 'border-indigo-400 text-indigo-700 dark:text-indigo-300' : 'border-zinc-300 text-zinc-600 dark:border-zinc-700 dark:text-zinc-400'}`}
                onClick={() => toggleFiles(!showFiles)}
              >
                📎 Files
              </button>
              <button className="text-xs text-zinc-500 underline" onClick={() => exportJson(detail)}>
                Export JSON
              </button>
            </div>
          </header>
          <Requests requests={detail.requests ?? []} />
          <Workers workers={detail.workers ?? []} />
          <Pins
            sessionId={id}
            refreshKey={`${detail.messages.length}:${pinsKey}`}
            open={openPin}
            onOpen={setOpenPin}
            onCount={onPinCount}
          />
        </div>
        <MobileSessionBar
          bot={detail.session.bot}
          title={detail.session.title}
          open={sheetOpen}
          onOpenChange={setSheetOpen}
          options={
            models && <ModelPicker models={models} value={detail.session.model ?? ''} onPick={pickModel} large />
          }
          summary={summary}
          meta={
            <>
              {detail.session.role || 'session'} · {(detail.message_count ?? detail.messages.length).toLocaleString()}{' '}
              messages · Updated {agoLong(lastActivity)} · Session{' '}
              {archived ? 'archived' : closed ? 'closed' : 'active'}
              {detail.session.started_by && detail.session.started_by_id && (
                <>
                  {' · started by '}
                  <Link className="underline" to={`/s/${detail.session.started_by_id}`}>
                    {detail.session.started_by}
                  </Link>
                </>
              )}
            </>
          }
          resolved={
            archived && detail.session.resolved_summary ? (
              <div className="mb-2 text-sm text-success">
                ✓ Resolved{detail.session.resolved_by ? ` by ${detail.session.resolved_by}` : ''}:{' '}
                <span className="text-ink">{detail.session.resolved_summary}</span>
              </div>
            ) : null
          }
          actions={
            <>
              {detail.session.role === 'thread' && (
                <button
                  type="button"
                  disabled={archived || busy}
                  title={archived ? 'Resolved; a new message reopens it' : 'Mark this thread resolved'}
                  onClick={async () => {
                    await api.close(id).catch(e => setError(String(e.message ?? e)))
                    document.querySelector<HTMLDialogElement>('#chat-session-sheet')?.close()
                    await load()
                    onChange()
                  }}
                >
                  {archived ? 'Resolved' : 'Resolve'}
                </button>
              )}
              <button
                type="button"
                aria-pressed={showFiles}
                onClick={() => {
                  toggleFiles(!showFiles)
                  document.querySelector<HTMLDialogElement>('#chat-session-sheet')?.close()
                }}
              >
                Files
              </button>
              <button
                type="button"
                onClick={() => {
                  exportJson(detail)
                  document.querySelector<HTMLDialogElement>('#chat-session-sheet')?.close()
                }}
              >
                Export JSON
              </button>
            </>
          }
        >
          <Requests requests={detail.requests ?? []} />
          <Workers workers={detail.workers ?? []} />
          <Pins
            sessionId={id}
            refreshKey={`${detail.messages.length}:${pinsKey}`}
            open={openPin}
            onOpen={pin => {
              setOpenPin(pin)
              if (pin) document.querySelector<HTMLDialogElement>('#chat-session-sheet')?.close()
            }}
            onCount={onPinCount}
          />
        </MobileSessionBar>
        <PinnedDashboard pin={openPin} refreshKey={detail.messages.length} onClose={() => setOpenPin(null)} />
        <div
          ref={transcript}
          onScroll={onTranscriptScroll}
          className={`chat-transcript relative min-h-24 flex-1 overflow-y-auto px-4 py-5 sm:px-6 ${openPin ? 'hidden' : ''}`}
        >
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
          <Transcript
            messages={detail.messages}
            calls={detail.calls}
            files={files}
            complete={!detail.has_more}
            sent={detail.sent}
            workers={detail.workers}
          />
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
          {scrolledUp && (
            <div className="pointer-events-none sticky bottom-2 flex h-0 justify-center">
              <button
                className="pointer-events-auto -translate-y-full rounded-full border border-stroke bg-surface px-3 py-1 text-xs text-ink shadow-md hover:bg-raised"
                title="Jump to the latest message"
                onClick={() => toBottom('smooth')}
              >
                ↓ {unseen ? 'New messages' : 'Jump to bottom'}
              </button>
            </div>
          )}
        </div>
        {!!waiting.length && (
          <div className="mx-4 mb-3 grid max-h-[45dvh] gap-2 overflow-y-auto sm:mx-6">
            {waiting.map(approval => (
              <section key={approval.id} className="rounded-card border border-warning/40 bg-amber-tint p-4 text-sm">
                <div className="font-medium">Tool approval requested: {approval.name}</div>
                <p className="mt-1 text-muted">Review this tool call; the composer waits for its decision.</p>
                {approval.preview && (
                  <pre className="mt-3 max-h-56 overflow-auto whitespace-pre-wrap rounded-control border border-warning/30 bg-canvas p-3 text-xs text-ink">
                    {approval.preview}
                  </pre>
                )}
                <div className="mt-3 flex gap-2">
                  <button
                    disabled={busy || !!pending}
                    className="rounded-control bg-warning px-4 py-2 font-semibold text-canvas disabled:opacity-50"
                    onClick={() => run(() => api.approve(id, true, [approval.id]))}
                  >
                    Approve
                  </button>
                  <button
                    disabled={busy || !!pending}
                    className="rounded-control border border-warning/50 px-4 py-2 disabled:opacity-50"
                    onClick={() => run(() => api.approve(id, false, [approval.id]))}
                  >
                    Reject
                  </button>
                </div>
              </section>
            ))}
          </div>
        )}
        {error && (
          <div className="mx-4 mb-2 rounded-card border border-danger/40 bg-red-tint p-3 text-sm text-danger sm:mx-6">
            {error}
          </div>
        )}
        {failedCall && (
          <div className="mx-4 mb-2 rounded-card border border-danger/40 bg-red-tint px-4 py-3 text-sm sm:mx-6">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium text-red-700 dark:text-red-300">
                {failedCall.error_summary || 'The last turn failed.'}
              </span>
              {failedCall.error_hint && (
                <span className="text-zinc-600 dark:text-zinc-400">
                  {detail?.retry_model
                    ? `It hit its subscription limit. Wait for the reset and Resume, or retry on ${detail.retry_model}.`
                    : failedCall.error_hint}
                </span>
              )}
              <span className="ml-auto flex gap-2">
                {detail?.retry_model && (
                  <button
                    disabled={busy}
                    className="rounded-control border border-accent px-3 py-1 text-xs font-semibold text-accent disabled:opacity-50"
                    title="Move this chat to that model and continue from where the last turn stopped"
                    onClick={() => run(() => api.resume(id, detail.retry_model), '')}
                  >
                    Retry on {detail.retry_model}
                  </button>
                )}
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
        {!!turnSuggestions.length && (
          <div className="suggestion-desktop mx-4 mb-2 flex flex-wrap gap-2 sm:mx-6">
            {turnSuggestions.map(s => (
              <button
                key={s}
                type="button"
                disabled={busy}
                className="rounded-full border border-indigo-300 px-3 py-1 text-sm text-indigo-700 hover:bg-indigo-50 disabled:opacity-50 dark:border-indigo-700 dark:text-indigo-300 dark:hover:bg-indigo-950"
                onClick={() => send(s, 'send', 'suggestion')}
              >
                {s}
              </button>
            ))}
          </div>
        )}
        <SuggestionChips suggestions={mobileSuggestions} disabled={busy} onPick={s => send(s, 'send', 'suggestion')} />
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
          className="chat-composer mx-4 mb-2 rounded-panel border border-stroke bg-surface p-4"
          onSubmit={e => {
            e.preventDefault()
            send(text)
          }}
        >
          <input ref={picker} type="file" multiple hidden onChange={e => attach(e.target.files)} />
          <textarea
            ref={composer}
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
            aria-label="Message"
            placeholder={
              closed
                ? 'This session is closed'
                : waiting.length
                  ? 'Answer the approval first'
                  : archived
                    ? 'Resolved: sending a message reopens it'
                    : running
                      ? 'Steer the bot: your message joins this turn'
                      : 'Message the bot'
            }
            className="block w-full resize-none rounded-card border border-stroke bg-raised px-3 py-2 focus:outline-none"
          />
          <div className="chat-composer-actions mt-3 flex flex-wrap items-center gap-2 border-t border-stroke pt-3">
            <button
              type="button"
              disabled={closed || !!waiting.length || uploading}
              title="Attach images, PDFs or other files"
              aria-label="Attach images, PDFs or other files"
              className="chat-attach rounded-control px-2 py-2 text-sm font-semibold text-mint hover:bg-raised disabled:opacity-50"
              onClick={() => picker.current?.click()}
            >
              <span className="chat-attach-label">{uploading ? 'Attaching…' : '+ Attach'}</span>
            </button>
            {models && <ModelLink label={modelLabel(models, detail.session.model ?? '')} onOpen={openOptions} />}
            <div className="chat-composer-send ml-auto flex items-center gap-2">
              {running && (
                <>
                  <button
                    type="button"
                    disabled={stopping}
                    title="Stop the bot after the step it's on"
                    className="rounded-control border border-stroke bg-raised px-3 py-2 text-sm disabled:opacity-50"
                    onClick={stop}
                  >
                    Stop
                  </button>
                  {(!!text.trim() || !!outgoing.length) && (
                    <button
                      type="button"
                      disabled={busy || uploading || stopping}
                      title="Stop the bot and answer this message instead"
                      className="rounded-control bg-accent px-3 py-2 text-sm font-semibold text-canvas disabled:opacity-50"
                      onClick={() => send(text, 'interrupt')}
                    >
                      Stop &amp; send
                    </button>
                  )}
                </>
              )}
              <button
                type="submit"
                disabled={busy || uploading || (!text.trim() && !outgoing.length)}
                title={running ? 'Send now; the bot sees it after the step it is on' : 'Send'}
                aria-label="Send"
                className="chat-send rounded-control bg-accent px-5 py-2.5 font-semibold text-canvas disabled:opacity-50"
              >
                Send
              </button>
            </div>
          </div>
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

// Long-running work this chat started: running ones with their latest progress and output
// (the full log a click away), and the last few that finished (their results also arrive
// as messages).
function Workers({ workers }: { workers: Worker[] }) {
  const [showDone, setShowDone] = useState(false)
  const running = workers.filter(w => w.status === 'queued' || w.status === 'running')
  const finished = workers.filter(w => !running.includes(w))
  const done = finished.slice(-5)
  const hiddenDone = finished.length - done.length
  if (!workers.length) return null
  return (
    <div className="mx-4 mb-2 flex flex-col gap-2 rounded-card border border-stroke bg-surface px-4 py-3 text-xs sm:mx-6">
      {running.map(w => (
        <div key={w.id} className="min-w-0">
          <div className="flex items-center gap-2 truncate">
            {w.status === 'running' ? (
              <span className="h-2.5 w-2.5 shrink-0 animate-spin rounded-full border-[1.5px] border-indigo-500 border-t-transparent" />
            ) : (
              <span className="text-zinc-400">{WORKER_ICON[w.status]}</span>
            )}
            <span className="font-medium">{w.title}</span>
            <span className="truncate text-zinc-500">{w.progress || w.status}</span>
            <span className="ml-auto" />
            <WorkerPulse worker={w} />
          </div>
          <WorkerActivityView worker={w} lines={2} />
        </div>
      ))}
      {!!done.length && (
        <button className="self-start text-zinc-400 hover:text-zinc-600" onClick={() => setShowDone(s => !s)}>
          {showDone ? '▾' : '▸'} {done.length} finished worker{done.length === 1 ? '' : 's'}
          {hiddenDone ? ` (latest; ${hiddenDone} earlier)` : ''}
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
    <div className="mx-4 mb-2 flex flex-col gap-2 rounded-card border border-teal/40 bg-teal-tint px-4 py-3 text-xs sm:mx-6">
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
