import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Link, useLocation, useParams, useSearchParams } from 'react-router-dom'
import type {
  AttachmentFile,
  Call,
  DelegatedRequest,
  IllustrationResult,
  Message,
  Pin,
  PrLink,
  RecurringWake,
  RealtimeVoiceSession,
  SentCard,
  SessionDetail,
  Todo,
  Turn,
  Wake,
  Worker,
} from '../api'
import { Composer as ChatComposer, type Attachment } from '@ergo/chat-ui'
import { api } from '../api'
import {
  ambiguousSend,
  chooseApproval,
  fromAttachment,
  toAttachment,
  type ApprovalChoices,
  type Outgoing,
} from '../chatAdapter'
import { ApprovalPanel } from '../components/ApprovalPanel'
import { useDraft } from '../draft'
import { prepareChatSend, type ChatSendSource } from '../chatSend'
import { lastTurn, suggestionsFromMessages, statusSummary } from '../chatLayout'
import { ActivityStrip } from '../components/ActivityStrip'
import Files from '../components/Files'
import { DebugShortcut } from '../components/DebugShortcut'
import { IllustrateDialog } from '../components/IllustrateDialog'
import { ComposerVoice, useComposerAttachments, useComposerVoice } from '../components/VoiceRecorder'
import Markdown from '../components/Markdown'
import ModelPicker, { EffortSlider, modelLabel, useBotModels } from '../components/ModelPicker'
import { PoolPicker, PoolRefusalBanner } from '../components/Pools'
import { PageViewer, Pins } from '../components/Pins'
import { AttachmentView, isAudio, replySuggestions, Transcript } from '../components/Transcript'
import { ModelLink, MobileSessionBar, SessionTitle, SuggestionChips, ThreadOptions } from '../components/ChatChrome'
import { TodoButton, TodoList } from '../components/TodoList'
import { WakeNotice } from '../components/WakeNotice'
import { RecurringWakeNotice } from '../components/RecurringWakeNotice'
import { usePersistedFlag } from '../persistedFlag'
import { useSessionUsage } from '../components/UsageSparkline'
import { UsageHistory } from '../components/UsageHistory'
import { agoLong } from '../time'
import { linkedLine, messageAnchor } from '../messageLink'
import {
  captureScrollAnchor,
  elementVisibleInRoot,
  historyScrollRoot,
  nearBottom,
  restoreScrollAnchor,
  shouldLoadOlder,
  type OlderLoadTrigger,
  type ScrollAnchor,
} from '../seeMore'

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

function OptionIcon({ path }: { path: string }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      width="20"
      height="20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d={path} />
    </svg>
  )
}

/** Whether a user message with this text was stored after ``line``. */
function stored(messages: Message[], text: string, line: number): boolean {
  const needle = JSON.stringify(text).slice(1, -1)
  return messages.some(m => m.line > line && m.role === 'user' && JSON.stringify(m.blocks).includes(needle))
}

/** The selected pin replaces the transcript while keeping the composer available. */
export function PinnedDashboard({
  pin,
  bot,
  sessionId,
  refreshKey,
  onClose,
}: {
  pin: Pin | null
  bot: string
  sessionId: string
  refreshKey: unknown
  onClose: () => void
}) {
  return pin ? <PageViewer pin={pin} bot={bot} sessionId={sessionId} refreshKey={refreshKey} onClose={onClose} /> : null
}

export function Chat({ onChange }: { onChange: () => void }) {
  const { id = '' } = useParams()
  const [detail, setDetail] = useState<SessionDetail | null>(null)
  const [voiceSessions, setVoiceSessions] = useState<RealtimeVoiceSession[]>([])
  useEffect(() => {
    let alive = true
    let timer: number | undefined
    setVoiceSessions([])
    const refresh = () => {
      api
        .realtimeVoiceSessions(id)
        .then(sessions => {
          if (alive) setVoiceSessions(sessions)
        })
        .catch(() => {})
    }
    api
      .realtimeVoiceConfig()
      .then(config => {
        if (!alive || !config.enabled) return
        refresh()
        timer = window.setInterval(refresh, 3000)
      })
      .catch(() => {})
    return () => {
      alive = false
      if (timer !== undefined) window.clearInterval(timer)
    }
  }, [id])
  const [text, saveText] = useDraft(id)
  const [busy, setBusy] = useState(false)
  const [showTodos, setShowTodos] = usePersistedFlag('ergonaut.todos')
  const [error, setError] = useState('')
  // Files picked or pasted for the next message, already uploaded to the session.
  const [outgoing, setOutgoing] = useComposerAttachments<Outgoing>(id)
  // Files sent with the queued message, shown with it until the worker stores it.
  const [sentFiles, setSentFiles] = useState<Outgoing[]>([])
  // The chat's files, so the transcript can show the ones a bot made where it made them.
  const [files, setFiles] = useState<AttachmentFile[]>([])
  const [uploading, setUploading] = useState(false)
  const [illustration, setIllustration] = useState<IllustrationResult | null>(null)
  // A turn a worker is running: what was sent, and how the calls and messages looked then.
  const [pending, setPending] = useState<{ text: string; lastCall: string; line: number; approvals: string } | null>(
    null,
  )
  // Stop was pressed; the turn ends at its next step.
  const [stopping, setStopping] = useState(false)
  // Answers to the tool calls a turn waits on, sent together once every one has one.
  const [choices, setChoices] = useState<ApprovalChoices>({ batch: '', choices: {} })
  // The Files panel stays open or closed across sessions, per browser.
  const [showFiles, toggleFiles] = usePersistedFlag('ergonaut.files')
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
  const [poolsChanged, setPoolsChanged] = useState(0)
  const models = useBotModels(detail?.session.bot ?? '', id, poolsChanged)
  const usage = useSessionUsage(id, detail?.messages.length)
  function openOptions() {
    if (window.matchMedia('(max-width: 640px)').matches) setSheetOpen(true)
    else setMenuOpen(true)
  }
  // Same call whichever picker (header dropdown or phone sheet) chose the model.
  async function pickModel(model: string) {
    await api.setModel(id, model).catch(e => setError(String(e.message ?? e)))
    await load()
  }
  async function pickEffort(effort: string) {
    await api.setEffort(id, effort).catch(e => setError(String(e.message ?? e)))
    await load()
  }
  async function renameSession(title: string) {
    const renamed = await api.renameSession(id, title)
    setDetail(d => (d && d.session.id === id ? { ...d, session: { ...d.session, title: renamed.title } } : d))
    onChange()
  }
  const composer = useRef<HTMLTextAreaElement>(null)
  const { setText, submit, voiceActive, voice } = useComposerVoice({
    draftKey: id,
    text,
    setText: saveText,
    composer,
    onSend: send,
    canSubmit: !busy && !uploading,
  })
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

  // Older messages of a long chat, a page at a time. The view stays on the message that was in
  // frame; See more loads itself when that button scrolls into the transcript, and stays clickable.
  const transcript = useRef<HTMLDivElement>(null)
  const seeMore = useRef<HTMLButtonElement>(null)
  const keepScroll = useRef<ScrollAnchor | null>(null)
  const skipScroll = useRef(false)
  // The view follows new messages only while it's at the bottom; scrolled up, it stays put.
  const atBottom = useRef(true)
  const [scrolledUp, setScrolledUp] = useState(false)
  const [unseen, setUnseen] = useState(false)
  // The newest line the reader has had in view; older pages loading in don't count as new.
  const seenLine = useRef(-1)
  // One older-page request at a time. `flight` drops a response after a chat switch or unmount.
  const flight = useRef(0)
  const olderInFlight = useRef(false)
  const olderFailed = useRef(false)
  const placed = useRef(false)
  const seeMoreInView = useRef(false)
  const [loadingOlder, setLoadingOlder] = useState(false)
  const [olderError, setOlderError] = useState(false)
  const loadOlderRef = useRef<(trigger: OlderLoadTrigger) => Promise<void>>(async () => {})

  function buttonVisible(): boolean {
    const root = transcript.current
    const button = seeMore.current
    if (!root || !button) return false
    const view = root.getBoundingClientRect()
    const box = button.getBoundingClientRect()
    // Not laid out yet (or hidden): the observer flag is the visibility signal.
    if (view.height === 0 || box.height === 0) return seeMoreInView.current
    return elementVisibleInRoot(historyScrollRoot(root), button)
  }
  function onTranscriptScroll() {
    const el = transcript.current
    if (!el) return
    atBottom.current = nearBottom(el)
    setScrolledUp(!atBottom.current)
    if (atBottom.current) setUnseen(false)
    if (window.matchMedia('(max-width: 640px)').matches) {
      const next = el.scrollTop > 32 ? '1' : ''
      if (document.documentElement.dataset.chatScrolled !== next) document.documentElement.dataset.chatScrolled = next
    }
    // The observer can report the button before this scroll updates atBottom.
    if (!atBottom.current) void loadOlderRef.current('auto')
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
  async function loadOlder(trigger: OlderLoadTrigger = 'manual') {
    const current = latest.current
    const visible = trigger === 'manual' ? true : buttonVisible()
    if (
      !shouldLoadOlder({
        hasMore: Boolean(current?.session.id === id && current.has_more && current.first_line != null),
        inFlight: olderInFlight.current,
        failed: olderFailed.current,
        placed: placed.current,
        atBottom: atBottom.current,
        visible,
        trigger,
      }) ||
      !current ||
      current.first_line == null ||
      current.session.id !== id
    )
      return
    if (trigger === 'manual') {
      olderFailed.current = false
      setOlderError(false)
    }
    const sessionId = id
    const before = current.first_line
    const ticket = flight.current
    olderInFlight.current = true
    setLoadingOlder(true)
    const el = transcript.current
    try {
      const page = await api.session(sessionId, before)
      if (ticket !== flight.current || latest.current?.session.id !== sessionId) return
      if (latest.current.first_line !== before) return
      if (page.has_more && (page.first_line == null || page.first_line >= before)) {
        throw new Error('History pagination did not advance.')
      }
      if (el && transcript.current === el) keepScroll.current = captureScrollAnchor(el)
      olderFailed.current = false
      setOlderError(false)
      setDetail(d =>
        d && d.session.id === sessionId && d.first_line === before
          ? { ...merge(d, page.messages, page.calls), first_line: page.first_line, has_more: page.has_more }
          : d,
      )
    } catch (e) {
      if (ticket !== flight.current || latest.current?.session.id !== sessionId) return
      olderFailed.current = true
      setOlderError(true)
      const message = e instanceof Error ? e.message : String(e)
      setError(`${message} Press See more to try again.`)
    } finally {
      if (ticket === flight.current) {
        olderInFlight.current = false
        setLoadingOlder(false)
      }
    }
  }
  loadOlderRef.current = loadOlder
  useLayoutEffect(() => {
    const el = transcript.current
    const anchor = keepScroll.current
    if (!anchor || !el) return
    restoreScrollAnchor(el, anchor)
    keepScroll.current = null
    skipScroll.current = true
    atBottom.current = nearBottom(el)
    setScrolledUp(!atBottom.current)
  }, [detail])

  useEffect(() => {
    flight.current += 1
    olderInFlight.current = false
    olderFailed.current = false
    seeMoreInView.current = false
    placed.current = false
    setLoadingOlder(false)
    setOlderError(false)
    setError('')
    setDetail(null)
    atBottom.current = true
    setScrolledUp(false)
    setUnseen(false)
    setOpenPin(null)
    setFiles([])
    const ticket = flight.current
    load().catch(e => {
      if (ticket === flight.current) setError(String(e.message ?? e))
    })
    return () => {
      flight.current += 1
      olderInFlight.current = false
    }
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
    setIllustration(null)
  }, [id])
  useEffect(() => {
    return () => {
      delete document.documentElement.dataset.chatScrolled
    }
  }, [])
  useLayoutEffect(() => {
    const el = composer.current
    if (!el) return
    const max = window.matchMedia('(max-width: 640px)').matches ? 128 : 200
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, max)}px`
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
      const { messages, calls, requests, title, workers, sent, prs, todos, wakes, recurring_wakes } = JSON.parse(
        e.data,
      ) as {
        messages: Message[]
        calls: Call[]
        requests?: DelegatedRequest[]
        title?: string
        workers?: Worker[]
        sent?: SentCard[]
        prs?: PrLink[]
        todos?: Todo[]
        wakes?: Wake[]
        recurring_wakes?: RecurringWake[]
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
              ...(todos ? { todos } : {}),
              ...(wakes ? { wakes } : {}),
              ...(recurring_wakes ? { recurring_wakes } : {}),
            }
          : d,
      )
      if (requests) onChange()
    }
    return () => events.close()
  }, [id, loaded])

  // A link to one message (/s/<id>#m-<line>): load older pages back to it, then bring it into view.
  // The transcript stays there (not following new messages) until the reader scrolls down.
  const target = linkedLine(useLocation().hash)
  const [reveal, setReveal] = useState<number | null>(null)
  useEffect(() => {
    if (target == null || !loaded) return
    atBottom.current = false
    let cancelled = false
    const sessionId = id
    const start = latest.current
    const needsPages = Boolean(start?.has_more && start.first_line != null && start.first_line > target)
    const ticket = needsPages ? ++flight.current : flight.current
    if (needsPages) {
      olderInFlight.current = true
      setLoadingOlder(true)
    }
    ;(async () => {
      try {
        let d = latest.current
        while (d && d.session.id === sessionId && d.has_more && d.first_line != null && d.first_line > target) {
          const before = d.first_line
          const page = await api.session(sessionId, before, 200)
          if (cancelled || ticket !== flight.current) return
          if (latest.current?.session.id !== sessionId) return
          if (page.has_more && (page.first_line == null || page.first_line >= before)) {
            throw new Error('History pagination did not advance.')
          }
          const add = (prev: SessionDetail) => ({
            ...merge(prev, page.messages, page.calls),
            first_line: page.first_line,
            has_more: page.has_more,
          })
          d = add(d)
          setDetail(prev => (prev && prev.session.id === sessionId && prev.first_line === before ? add(prev) : prev))
        }
        if (!cancelled && ticket === flight.current) setReveal(target)
      } catch (e) {
        if (cancelled || ticket !== flight.current) return
        olderFailed.current = true
        setOlderError(true)
        const message = e instanceof Error ? e.message : String(e)
        setError(`${message} Press See more to try again.`)
      } finally {
        if (needsPages && ticket === flight.current) {
          olderInFlight.current = false
          setLoadingOlder(false)
        }
      }
    })()
    return () => {
      cancelled = true
      if (needsPages) {
        flight.current += 1
        olderInFlight.current = false
        setLoadingOlder(false)
      }
    }
  }, [id, loaded, target])
  useLayoutEffect(() => {
    const box = transcript.current
    if (reveal == null || !box || !detail) return
    setReveal(null)
    // The linked message, or the first one shown after it (a folded or hidden line has no box).
    const el = detail.messages
      .filter(m => m.line >= reveal)
      .map(m => document.getElementById(messageAnchor(m.line)))
      .find(node => node && node.offsetHeight > 0)
    if (!el) return
    const at = el.getBoundingClientRect()
    const view = box.getBoundingClientRect()
    if (at.top >= view.top && at.bottom <= view.bottom) return
    box.scrollTop += at.top - view.top - 24
    atBottom.current = false
    setScrolledUp(true)
  }, [reveal, detail])

  useEffect(() => {
    if (skipScroll.current) {
      skipScroll.current = false
      placed.current = true
      return
    }
    // Opening a chat pins to the latest message. A scroll event before that must not look like
    // the reader scrolled up and start paging history. A message link opts out: it stays put.
    if (!placed.current && target == null) {
      atBottom.current = true
      setScrolledUp(false)
    }
    if (atBottom.current) pinToBottom()
    placed.current = true
    const newest = detail?.messages[detail.messages.length - 1]?.line ?? -1
    if (!atBottom.current && newest > seenLine.current && seenLine.current >= 0) setUnseen(true)
    seenLine.current = newest
  }, [detail, busy, target])

  // See more loads itself once it is inside the transcript scroller. Phone and desktop both scroll
  // that element, not the page; a scrollable ancestor is used if the layout ever moves the root.
  useEffect(() => {
    const box = transcript.current
    const button = seeMore.current
    if (!box || !button || !detail?.has_more || openPin) return
    if (typeof IntersectionObserver === 'undefined') return
    const root = historyScrollRoot(box)
    const observer = new IntersectionObserver(
      entries => {
        const visible = entries.some(entry => entry.isIntersecting && entry.target === button)
        seeMoreInView.current = visible
        if (visible) void loadOlderRef.current('auto')
      },
      { root, rootMargin: '0px', threshold: 0 },
    )
    observer.observe(button)
    return () => observer.disconnect()
  }, [id, detail?.has_more, detail?.first_line, loadingOlder, openPin])

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
    const call = lastTurn(detail.calls)
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
  const lastCallId = lastTurn(detail?.calls ?? [])?.id
  const lastStatus = lastTurn(detail?.calls ?? [])?.status
  useEffect(() => {
    if (lastStatus !== 'in_progress') setStopping(false)
  }, [lastCallId, lastStatus])

  async function run(action: () => Promise<Turn>, sent = '', accepted?: () => void) {
    setBusy(true)
    setError('')
    const calls = detail?.calls ?? []
    const messages = detail?.messages ?? []
    const before = {
      text: sent,
      lastCall: lastTurn(calls)?.id ?? '',
      line: messages.length ? messages[messages.length - 1].line : -1,
      approvals: JSON.stringify(lastTurn(calls)?.pending_approvals ?? []),
    }
    let acknowledged = false
    try {
      const turn = await action()
      acknowledged = true
      accepted?.()
      if (turn.queued) {
        setPending(before)
      } else {
        if (turn.error) setError(turn.error)
        await load()
        onChange()
      }
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e)
      setError(accepted && !acknowledged ? ambiguousSend(message) : message)
      if (accepted && !acknowledged) throw new Error(ambiguousSend(message))
    } finally {
      setBusy(false)
    }
  }

  // While a turn runs, a message steers it ("send") or stops it and starts the next one ("interrupt").
  function send(message: string, mode: 'send' | 'interrupt' = 'send', source: ChatSendSource = 'composer') {
    const plan = prepareChatSend(source, message, text, outgoing)
    if ((!plan.message.trim() && !plan.attachmentIds.length) || busy || uploading) return
    if (mode === 'interrupt') setStopping(true)
    toBottom()
    return run(
      () => api.send(id, plan.message, plan.attachmentIds, mode),
      plan.message || plan.sentFiles.map(f => f.filename).join(', '),
      plan.composer === 'clear'
        ? () => {
            setText(current => (current === message ? '' : current))
            setSentFiles(plan.sentFiles)
            setOutgoing(current => current.filter(file => !plan.attachmentIds.includes(file.id)))
          }
        : undefined,
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

  // The composer hands over the picked or pasted files; what was stored comes back to it as attachments.
  // A failure part-way keeps the files already stored (they are on the server) and reports the error.
  async function attach(files: File[]): Promise<Attachment[]> {
    if (!files.length) return []
    setUploading(true)
    setError('')
    const stored: Attachment[] = []
    try {
      for (const file of files) {
        const saved = await api.uploadAttachment(id, file)
        stored.push(
          toAttachment({
            id: saved.id,
            filename: saved.filename,
            image: saved.kind === 'image' || saved.view === 'image',
            audio: isAudio(saved),
          }),
        )
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setUploading(false)
    }
    return stored
  }

  // Dropped at once; the server copy is deleted best-effort.
  async function unattach(attachment: Attachment) {
    setOutgoing(list => list.filter(f => f.id !== attachment.id))
    await api.deleteAttachment(attachment.id).catch(() => undefined)
  }

  if (!detail) return <div className="p-6 text-zinc-500">{error || 'Loading…'}</div>

  const lastCall = lastTurn(detail.calls)
  const waiting = lastCall?.status === 'awaiting_approval' ? lastCall.pending_approvals : []
  async function revalidate() {
    if (!lastCall || busy || pending) return
    setBusy(true)
    setError('')
    try {
      await api.revalidateApproval(id, lastCall.id, waiting)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }
  function decide(approvalId: string, approve: boolean) {
    const { state, submit } = chooseApproval(choices, lastCall?.id, waiting, approvalId, approve)
    setChoices(state)
    if (submit) run(() => api.decide(id, submit, waiting, lastCall?.id))
  }
  // A newer user message or an unfinished call supersedes the previous reply.
  const newerUserMessage = detail.messages.some(
    m =>
      m.role === 'user' &&
      m.blocks.some(block => block.type === 'text' || block.type === 'attachment') &&
      lastCall?.last_sequence != null &&
      m.line > lastCall.last_sequence,
  )
  const suggestions =
    waiting.length || newerUserMessage || (lastCall && lastCall.status !== 'completed')
      ? []
      : replySuggestions(
          lastCall?.response?.suggestions ??
            suggestionsFromMessages(
              lastCall?.first_sequence == null
                ? detail.messages
                : detail.messages.filter(m => m.line >= lastCall.first_sequence!),
            ),
        )
  const summary = statusSummary(
    detail.requests ?? [],
    detail.workers ?? [],
    pinCount,
    detail.todos ?? [],
    detail.wakes ?? [],
  )
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

  const closeOptions = () => {
    setMenuOpen(false)
    setSheetOpen(false)
  }
  const threadOptions = (
    <div className="thread-option-groups">
      <section aria-label="Conversation">
        <h3>Conversation</h3>
        <Link to={`/s/${id}/voice`} onClick={closeOptions} className="thread-option-action">
          <OptionIcon path="M9 5a3 3 0 0 1 6 0v6a3 3 0 0 1-6 0V5Zm-3 6a6 6 0 0 0 12 0M12 17v5m-4 0h8" />
          <span>
            Voice call<small>Talk in this thread; start the microphone on the voice page.</small>
          </span>
        </Link>
        <button
          type="button"
          className="thread-option-action"
          aria-expanded={showFiles}
          aria-controls={showFiles ? 'chat-files' : undefined}
          onClick={() => {
            toggleFiles(!showFiles)
            closeOptions()
          }}
        >
          <OptionIcon path="M6 3h8l4 4v14H6V3Zm8 0v5h4M9 12h6m-6 4h6" />
          <span>
            {showFiles ? 'Hide files' : 'Files'}
            <small>Attachments and shared materials</small>
          </span>
        </button>
      </section>
      {models && (
        <section aria-label="Model and reasoning">
          <h3>Model & reasoning</h3>
          <div className="flex flex-col gap-2">
            <PoolPicker
              sessionId={id}
              onChanged={() => {
                setPoolsChanged(n => n + 1)
                load()
              }}
            />
            <ModelPicker models={models} value={detail.session.model ?? ''} onPick={pickModel} large />
            <EffortSlider value={detail.session.effort ?? ''} onPick={pickEffort} />
          </div>
        </section>
      )}
      <section aria-label="Chat permanence">
        <h3>Chat permanence</h3>
        <button
          type="button"
          className="thread-option-action"
          aria-pressed={!!detail.session.is_permanent}
          onClick={async () => {
            try {
              const session = await api.setPermanent(id, !detail.session.is_permanent)
              setDetail(d => (d ? { ...d, session } : d))
              onChange()
            } catch (e) {
              setError(String((e as Error).message ?? e))
            }
            closeOptions()
          }}
        >
          <span>
            {detail.session.is_permanent ? 'Remove permanence' : 'Make permanent'}
            <small>
              {detail.session.is_permanent ? 'This chat cannot be resolved' : 'Protect this chat from being resolved'}
            </small>
          </span>
        </button>
      </section>
      <section aria-label="Thread data">
        <h3>Thread data</h3>
        <button
          type="button"
          className="thread-option-action"
          onClick={() => {
            exportJson(detail)
            closeOptions()
          }}
        >
          <OptionIcon path="M12 3v12m-5-5 5 5 5-5M5 16v5h14v-5" />
          <span>
            Export JSON<small>Download this conversation</small>
          </span>
        </button>
      </section>
      <section aria-label="Chat lifecycle">
        <h3>Chat lifecycle</h3>
        <button
          type="button"
          className="thread-option-action thread-option-danger"
          disabled={archived || closed || busy || detail.session.is_permanent}
          onClick={async () => {
            await api.close(id).catch(e => setError(String(e.message ?? e)))
            closeOptions()
            await load()
            onChange()
          }}
        >
          <OptionIcon path="m7 12 3 3 7-7M21 12a9 9 0 1 1-9-9" />
          <span>
            {archived || closed ? 'Resolved' : detail.session.role === 'thread' ? 'Resolve thread' : 'Resolve chat'}
            <small>Archives this chat and stops its queued/running workers.</small>
          </span>
        </button>
      </section>
    </div>
  )

  return (
    <div className="chat-session relative flex h-full">
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="chat-desktop-chrome">
          <header className="chat-header flex flex-wrap items-center gap-3 px-4 py-4 sm:px-6">
            <div className="min-w-0">
              <div className="chat-title-bot truncate text-xs font-semibold uppercase tracking-wide text-muted">
                {detail.session.bot}
              </div>
              <SessionTitle key={id} title={detail.session.title} onSave={renameSession} />
              <div className="mt-1 text-sm text-muted">
                {detail.session.is_permanent && 'Permanent · '}
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
              {usage && <UsageHistory key={id} sessionId={id} usage={usage} />}
              <ThreadOptions open={menuOpen} onOpenChange={setMenuOpen}>
                {threadOptions}
              </ThreadOptions>
            </div>
          </header>
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
          titleEditor={<SessionTitle key={id} title={detail.session.title} onSave={renameSession} mobile />}
          open={sheetOpen}
          onOpenChange={setSheetOpen}
          options={threadOptions}
          summary={summary}
          meta={
            <>
              {detail.session.is_permanent && 'Permanent · '}
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
              {usage && (
                <div className="mt-2">
                  <UsageHistory key={id} sessionId={id} usage={usage} />
                </div>
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
          actions={null}
        >
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
        <ActivityStrip sessionId={id} requests={detail.requests ?? []} workers={detail.workers ?? []}>
          <Requests requests={detail.requests ?? []} onAnswered={load} />
        </ActivityStrip>
        <PinnedDashboard
          pin={openPin}
          bot={detail.session.bot}
          sessionId={id}
          refreshKey={detail.messages.length}
          onClose={() => setOpenPin(null)}
        />
        <div
          ref={transcript}
          onScroll={onTranscriptScroll}
          className={`chat-transcript relative min-h-24 flex-1 overflow-y-auto px-4 py-5 sm:px-6 ${openPin ? 'hidden' : ''}`}
        >
          {detail.has_more && (
            <div className="mb-4 flex justify-center">
              <button
                ref={seeMore}
                type="button"
                data-see-more
                disabled={loadingOlder}
                aria-busy={loadingOlder}
                title={olderError ? 'Could not load older messages. Press to try again.' : 'Load older messages'}
                className="rounded-full border border-stroke px-3 py-1 text-xs text-muted hover:bg-raised disabled:opacity-50"
                onClick={() => void loadOlder('manual')}
              >
                {loadingOlder ? 'Loading…' : 'See more'}
              </button>
              <span className="sr-only" aria-live="polite">
                {loadingOlder
                  ? 'Loading older messages'
                  : olderError
                    ? 'Could not load older messages. Press See more to try again.'
                    : ''}
              </span>
            </div>
          )}
          <Transcript
            messages={detail.messages}
            calls={detail.calls}
            compactions={detail.compactions}
            sessionId={detail.session.id}
            files={files}
            complete={!detail.has_more}
            sent={detail.sent}
            workers={detail.workers}
            highlight={target}
            voiceSessions={voiceSessions}
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
                  <AttachmentView id={file.id} label={file.filename} image={file.image} audio={file.audio} />
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
        <ApprovalPanel
          waiting={waiting}
          choices={choices.choices}
          disabled={busy || !!pending}
          onDecide={decide}
          onRevalidate={revalidate}
        />
        {error && (
          <div className="mx-4 mb-2 rounded-card border border-danger/40 bg-red-tint p-3 text-sm text-danger sm:mx-6">
            {error}
          </div>
        )}
        {detail?.session.pool_refusal && (
          <PoolRefusalBanner
            sessionId={id}
            refusal={detail.session.pool_refusal}
            busy={busy}
            onSwitched={() => {
              setPoolsChanged(n => n + 1)
              load()
            }}
          />
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
        <SuggestionChips
          suggestions={suggestions}
          disabled={busy || !!pending || uploading}
          onPick={s => send(s, 'send', 'suggestion')}
          variant="desktop"
        />
        <SuggestionChips
          suggestions={suggestions}
          disabled={busy || !!pending || uploading}
          onPick={s => send(s, 'send', 'suggestion')}
        />
        {illustration && (
          <aside className="mx-4 mt-2 flex items-center gap-3 rounded-card border border-indigo-300 bg-indigo-tint px-3 py-2 text-sm dark:border-indigo-700">
            <span aria-hidden="true">✦</span>
            <span className="min-w-0 flex-1">
              <strong>Illustration ready</strong> · {illustration.summary}{' '}
              <span className="text-muted">(as of #{illustration.as_of_line})</span>
            </span>
            <a
              className="shrink-0 font-medium text-indigo-700 underline dark:text-indigo-300"
              href={api.viewUrl(illustration.attachment.id)}
              target="_blank"
              rel="noreferrer"
            >
              Open
            </a>
          </aside>
        )}
        <WakeNotice wakes={detail.wakes ?? []} />
        <RecurringWakeNotice
          key={id}
          sessionId={id}
          schedules={detail.recurring_wakes ?? []}
          onChange={schedule =>
            setDetail(d =>
              d && d.session.id === id
                ? {
                    ...d,
                    recurring_wakes: (d.recurring_wakes ?? []).map(row => (row.id === schedule.id ? schedule : row)),
                  }
                : d,
            )
          }
        />
        {showTodos && <TodoList id="chat-todos" todos={detail.todos ?? []} />}
        <div className="chat-floating-actions mx-5 mt-2 mb-2 flex flex-wrap justify-end gap-2">
          <TodoButton
            todos={detail.todos ?? []}
            open={showTodos}
            controls="chat-todos"
            onToggle={() => {
              setShowTodos(!showTodos)
            }}
          />
          {detail.debug_shortcut && <DebugShortcut sessionId={id} />}
          <IllustrateDialog
            sessionId={id}
            disabled={closed || !!waiting.length || busy || uploading}
            onCreated={result => {
              setIllustration(result)
              setFiles(items => [result.attachment, ...items.filter(item => item.id !== result.attachment.id)])
            }}
          />
        </div>
        <ChatComposer
          textareaRef={composer}
          className="chat-composer mx-4 mb-2 rounded-panel border border-stroke bg-surface px-2 pt-1.5 pb-1 focus-within:border-accent/60"
          unstyled
          classNames={{
            row: 'chat-composer-row flex items-end gap-1',
            attach:
              'chat-attach flex h-8 w-8 shrink-0 items-center justify-center rounded-control text-xl leading-none text-mint hover:bg-raised disabled:opacity-50',
            input:
              'min-w-0 flex-1 resize-none overflow-y-auto bg-transparent px-1 py-1.5 leading-snug focus:outline-none',
            stop: 'chat-stop h-8 shrink-0 rounded-control border border-stroke bg-raised px-2 text-xs disabled:opacity-50',
            send: 'chat-send flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-accent text-canvas disabled:opacity-40',
            pending: 'flex flex-wrap gap-2 pb-1',
          }}
          draft={text}
          onDraftChange={setText}
          attachments={outgoing.map(toAttachment)}
          onAttachmentsChange={list => setOutgoing(list.map(fromAttachment))}
          onAttach={attach}
          onRemoveAttachment={unattach}
          attachDisabled={closed || !!waiting.length || uploading}
          disabled={closed || !!waiting.length}
          // Ergonaut clears the draft itself, and only what was sent (see send()); voice decides when to submit.
          clearOnSend={false}
          canSend={!(busy || uploading || (!text.trim() && !outgoing.length && !voiceActive))}
          onSend={() => submit()}
          busy={running}
          sendWhileBusy
          onStop={stop}
          stopDisabled={stopping}
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
          buttons={{ send: 'Send', stop: 'Stop', attach: 'Attach images, PDFs or other files' }}
          titles={{
            send: running ? 'Send now; the bot sees it after the step it is on' : 'Send',
            stop: "Stop the bot after the step it's on",
            attach: uploading ? 'Attaching…' : 'Attach images, PDFs or other files',
          }}
          icons={{
            attach: uploading ? '…' : '+',
            send: (
              <svg
                viewBox="0 0 16 16"
                width="16"
                height="16"
                aria-hidden="true"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M8 13V3M3.5 7.5 8 3l4.5 4.5" />
              </svg>
            ),
          }}
          renderPending={(file, remove) => (
            <span className="flex items-center gap-1 rounded-full border border-zinc-300 px-2 py-0.5 text-xs dark:border-zinc-700">
              {file.mediaType?.startsWith('image/') ? (
                <img src={file.url} alt="" className="h-6 w-6 rounded object-cover" />
              ) : (
                '📎'
              )}{' '}
              {file.name}
              <button type="button" className="text-zinc-400 hover:text-red-600" title="Remove" onClick={remove}>
                ✕
              </button>
            </span>
          )}
          afterInput={<ComposerVoice sessionId={id} disabled={closed || !!waiting.length} {...voice} />}
          actions={
            running && (!!text.trim() || !!outgoing.length || voiceActive) ? (
              <button
                type="button"
                disabled={busy || uploading || stopping}
                title="Stop the bot and answer this message instead"
                className="chat-stop h-8 shrink-0 rounded-control bg-accent px-2 text-xs font-semibold text-canvas disabled:opacity-50"
                onClick={() => void submit('interrupt')}
              >
                Stop &amp; send
              </button>
            ) : null
          }
          footer={
            models ? (
              <div className="chat-composer-meta flex px-1 leading-tight">
                <ModelLink label={modelLabel(models, detail.session.model ?? '')} onOpen={openOptions} />
              </div>
            ) : null
          }
        />
      </div>
      {showFiles && (
        <Files
          sessionId={id}
          refreshKey={files}
          onPinsChanged={() => setPinsKey(k => k + 1)}
          onClose={() => toggleFiles(false)}
          onView={pin => {
            setOpenPin(pin)
            // The phone rail overlays the viewer; uncover the selected file.
            if (window.matchMedia('(max-width: 640px)').matches) toggleFiles(false)
          }}
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
function Requests({ requests, onAnswered }: { requests: DelegatedRequest[]; onAnswered: () => void }) {
  const [answering, setAnswering] = useState('')
  const [failed, setFailed] = useState('')
  const open = requests.filter(r => r.status !== 'answered' && r.status !== 'failed')
  if (!open.length) return null
  // The other chat is paused on an approval: answer it from here, without leaving this chat.
  async function answer(r: DelegatedRequest, approve: boolean) {
    if (!r.other_session_id) return
    setAnswering(r.id)
    setFailed('')
    try {
      await api.decide(
        r.other_session_id,
        Object.fromEntries((r.approvals ?? []).map(a => [a.id, approve])),
        r.approvals ?? [],
        r.approval_call_id,
      )
      onAnswered()
    } catch (e) {
      setFailed(e instanceof Error ? e.message : String(e))
    } finally {
      setAnswering('')
    }
  }
  return (
    <div className="mb-2 flex flex-col gap-2 rounded-card border border-teal/40 bg-teal-tint px-4 py-3 text-xs">
      {open.map(r => (
        <div key={r.id}>
          <div className="flex items-center gap-2 truncate">
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
            <span className="text-zinc-500">
              ·{' '}
              {r.status === 'waiting' && r.direction === 'out' && !r.approvals?.length
                ? REQUEST_STATUS.delivered
                : REQUEST_STATUS[r.status]}
            </span>
            <span className="truncate text-zinc-400">“{r.text}”</span>
          </div>
          {r.direction === 'out' && !!r.approvals?.length && (
            <div className="mt-2 rounded-control border border-warning/40 bg-amber-tint p-2">
              {r.approvals.map(a => (
                <div key={a.id} className="mb-1">
                  <span className="font-medium">{a.name}</span>
                  <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap text-ink">
                    {JSON.stringify(a.input, null, 2)}
                  </pre>
                </div>
              ))}
              <div className="mt-1 flex items-center gap-2">
                <button
                  disabled={!!answering}
                  className="rounded-control bg-warning px-3 py-1 font-semibold text-canvas disabled:opacity-50"
                  onClick={() => answer(r, true)}
                >
                  {r.approvals.length > 1 ? 'Approve all' : 'Approve'}
                </button>
                <button
                  disabled={!!answering}
                  className="rounded-control border border-warning/50 px-3 py-1 disabled:opacity-50"
                  onClick={() => answer(r, false)}
                >
                  Reject
                </button>
                {failed && <span className="text-danger">{failed}</span>}
              </div>
            </div>
          )}
        </div>
      ))}
    </div>
  )
}
