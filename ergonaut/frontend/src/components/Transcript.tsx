import { Fragment, createContext, useContext, useState } from 'react'
import type { AttachmentFile, Block, Call, Message, SentCard, Worker } from '../api'
import { api } from '../api'
import { DirectoryContext } from './BotIcon'
import Markdown from './Markdown'
import { PrChip, ThreadCard, ThreadLink, WorkerCard, linkThreads } from './Threads'
import { ToolCard, pretty, resultText } from './ToolCard'

// Tools that send work to another chat or start a worker; their calls show as cards.
const THREAD_TOOLS = new Set(['ergo_thread_send', 'ergo_message_up'])
const WORKER_TOOLS = new Set(['orca_start_worker', 'ergo_worker_start'])

// What the transcript knows about the chat's delegated work (from the session API).
type Delegated = {
  cards: Map<string, SentCard> // by thread message id
  workers: Map<string, Worker> // by worker id
  known: Map<string, string> // chat id -> title, for links in the text
}
const DelegatedContext = createContext<Delegated>({ cards: new Map(), workers: new Map(), known: new Map() })

// Long runs of tool calls show their last few; the rest fold into a "+N more" row.
const VISIBLE_CALLS = 3

type FoldRow = { key: string; hidden: number; summary: string; open: boolean }
type Folds = {
  hidden: Set<string> // tool call ids folded away
  folded: Set<string> // tool call ids in a fold (shown as one-line rows when it's open)
  rows: Map<string, FoldRow> // by the id of the run's first call: where its "+N more" row goes
  durations: Map<string, number> // tool call id -> ms until its result
  toggle: (key: string) => void
}
const FoldContext = createContext<Folds>({
  hidden: new Set(),
  folded: new Set(),
  rows: new Map(),
  durations: new Map(),
  toggle: () => {},
})

/** "penpot_apply ×12, penpot_render ×4": the commonest tools in a fold. */
function foldSummary(names: string[]): string {
  const counts = new Map<string, number>()
  for (const name of names) counts.set(name, (counts.get(name) ?? 0) + 1)
  const top = [...counts.entries()].sort((a, b) => b[1] - a[1])
  const shown = top.slice(0, 3).map(([name, n]) => (n > 1 ? `${name} ×${n}` : name))
  return shown.join(', ') + (top.length > 3 ? ', …' : '')
}

/** Split each turn's tool calls into runs (broken by text, a reply, a user message or a new turn)
 *  and fold all but the last few of each. Calls waiting on approval, failed calls, and cards for
 *  work sent elsewhere always stay in view. */
function foldRuns(
  messages: Message[],
  results: Map<string, ToolResult>,
  pending: Set<string>,
  starts: Map<number, Call>,
  replies: string[],
  open: Set<string>,
): Pick<Folds, 'hidden' | 'folded' | 'rows'> {
  const hidden = new Set<string>()
  const folded = new Set<string>()
  const rows = new Map<string, FoldRow>()
  let run: ToolUse[] = []
  const close = () => {
    const pinned = (use: ToolUse) =>
      pending.has(use.id) || !!results.get(use.id)?.is_error || THREAD_TOOLS.has(use.name) || WORKER_TOOLS.has(use.name)
    const foldable = run.filter(use => !pinned(use)).slice(0, -VISIBLE_CALLS)
    if (foldable.length) {
      const key = run[0].id
      const isOpen = open.has(key)
      rows.set(key, { key, hidden: foldable.length, summary: foldSummary(foldable.map(u => u.name)), open: isOpen })
      for (const use of foldable) folded.add(use.id)
      if (!isOpen) for (const use of foldable) hidden.add(use.id)
    }
    run = []
  }
  for (const message of messages) {
    if (starts.has(message.line)) close()
    for (const block of message.blocks) {
      if (block.type === 'tool_use') {
        if (block.name === REPLY_TOOL) close()
        else run.push(block)
      } else if (block.type === 'text') {
        if (message.role !== 'user' && echoesReply(block.text, replies)) continue
        close()
      } else if (block.type === 'attachment') {
        close()
      }
    }
  }
  close()
  return { hidden, folded, rows }
}

/** How long each tool call took: from its message to its result's. */
function toolDurations(messages: Message[]): Map<string, number> {
  const called = new Map<string, number>()
  const durations = new Map<string, number>()
  for (const message of messages) {
    const at = message.timestamp ? Date.parse(message.timestamp) : NaN
    for (const block of message.blocks) {
      if (block.type === 'tool_use' && !Number.isNaN(at)) called.set(block.id, at)
      if (block.type === 'tool_result' && !Number.isNaN(at) && called.has(block.tool_use_id))
        durations.set(block.tool_use_id, at - called.get(block.tool_use_id)!)
    }
  }
  return durations
}

function FoldToggle({ row }: { row: FoldRow }) {
  const { toggle } = useContext(FoldContext)
  return (
    <button
      className="flex w-full items-center gap-2 rounded-control border border-dashed border-stroke px-2.5 py-1 text-left text-xs text-muted hover:border-accent hover:text-ink"
      onClick={() => toggle(row.key)}
    >
      <span className="w-3 text-center">{row.open ? '−' : '+'}</span>
      <span className="shrink-0 font-medium">
        {row.open ? `Hide ${row.hidden} earlier calls` : `${row.hidden} more call${row.hidden > 1 ? 's' : ''}`}
      </span>
      {!row.open && <span className="truncate font-mono">{row.summary}</span>}
    </button>
  )
}

/** A tool result's JSON object, if it is one. */
function resultJson(result?: ToolResult): Record<string, unknown> | null {
  if (!result || result.is_error) return null
  try {
    const value = JSON.parse(resultText(result.content))
    return value && typeof value === 'object' ? value : null
  } catch {
    return null
  }
}

/** A tool call: a card for work sent to another chat or a worker, otherwise the call itself. */
function ToolCall({ use, result, pending }: { use: ToolUse; result?: ToolResult; pending: boolean }) {
  const { cards, workers } = useContext(DelegatedContext)
  const { durations, folded } = useContext(FoldContext)
  const data = resultJson(result)
  const card = data && THREAD_TOOLS.has(use.name) ? cards.get(String(data.message_id ?? '')) : undefined
  if (card) return <ThreadCard card={card} />
  if (data && WORKER_TOOLS.has(use.name)) {
    const worker = workers.get(String(data.id ?? '')) ?? (data.title && data.status ? (data as Worker) : null)
    if (worker) return <WorkerCard worker={worker} />
  }
  return (
    <>
      <ToolCard
        use={use}
        result={result}
        pending={pending}
        duration={durations.get(use.id)}
        compact={folded.has(use.id)}
      />
      <ToolImages result={result} />
    </>
  )
}

/** Bot text with the chats it names (by id) as links. */
function BotMarkdown({ text }: { text: string }) {
  const { known } = useContext(DelegatedContext)
  return <Markdown text={known.size ? linkThreads(text, known) : text} />
}

type ToolResult = Extract<Block, { type: 'tool_result' }>
type ToolUse = Extract<Block, { type: 'tool_use' }>

const REPLY_TOOL = 'send_reply'

// A file in the chat: images as a thumbnail, anything else as a chip. Both open the file.
export function AttachmentView({
  id,
  label,
  image,
  note,
  link,
}: {
  id?: string
  label: string
  image?: boolean
  note?: string
  link?: AttachmentFile['link']
}) {
  if (link) return <PrChip pr={link} />
  if (id && image)
    return (
      <a href={api.viewUrl(id)} target="_blank" rel="noreferrer" title={note ? `${label} · ${note}` : label}>
        <img
          src={api.viewUrl(id)}
          alt={label}
          loading="lazy"
          className="max-h-60 max-w-full rounded-lg border border-zinc-300 dark:border-zinc-700"
        />
      </a>
    )
  return id ? (
    <a
      href={api.viewUrl(id)}
      target="_blank"
      rel="noreferrer"
      title={note}
      className="rounded-md border border-zinc-300 px-2 py-1 text-xs text-indigo-600 hover:underline dark:border-zinc-700 dark:text-indigo-400"
    >
      📎 {label}
    </a>
  ) : (
    <div className="rounded-md border border-zinc-300 px-2 py-1 text-xs text-zinc-500 dark:border-zinc-700">
      📎 {label}
    </div>
  )
}

type ImageRef = { type: 'image_ref'; attachment_id?: string; name?: string; media_type?: string }

// Images a tool returned (django_ergo.conversation.images): stored as image_ref items in its result.
function imageRefs(content: unknown): ImageRef[] {
  if (!Array.isArray(content)) return []
  return content.filter(
    (part): part is ImageRef => !!part && typeof part === 'object' && (part as ImageRef).type === 'image_ref',
  )
}

/** Ids of the files the transcript already shows: sent with a message, or returned by a tool. */
function shownFileIds(messages: Message[]): Set<string> {
  const ids = new Set<string>()
  for (const message of messages)
    for (const block of message.blocks) {
      if (block.type === 'attachment' && block.id) ids.add(block.id)
      if (block.type === 'tool_result')
        for (const ref of imageRefs(block.content)) if (ref.attachment_id) ids.add(ref.attachment_id)
    }
  return ids
}

/** Files a bot made (ergo_attachments_create, orca_attach, …), keyed by the last message written before each. */
function placeFiles(messages: Message[], files: AttachmentFile[], complete: boolean): Map<number, AttachmentFile[]> {
  const shown = shownFileIds(messages)
  const placed = new Map<number, AttachmentFile[]>()
  const stamped = messages.filter(m => m.timestamp)
  // Only part of a long chat is loaded: files from before it wait until it is.
  const loadedFrom = !complete && stamped.length ? Date.parse(stamped[0].timestamp!) : -Infinity
  for (const file of files) {
    if (file.source !== 'bot' || file.message_sequence != null || file.archived_at || shown.has(file.id)) continue
    const created = Date.parse(file.created_at)
    if (created < loadedFrom) continue
    let line = messages.length ? messages[messages.length - 1].line : -1
    const after = stamped.find(m => Date.parse(m.timestamp!) > created)
    if (after) {
      const index = messages.indexOf(after)
      line = index > 0 ? messages[index - 1].line : -1
    }
    placed.set(line, [...(placed.get(line) ?? []), file])
  }
  for (const list of placed.values()) list.sort((a, b) => a.created_at.localeCompare(b.created_at))
  return placed
}

function BotFiles({ files }: { files: AttachmentFile[] }) {
  return (
    <div className="mt-2 flex flex-col items-start gap-2">
      {files.map(file => (
        <AttachmentView
          key={file.id}
          id={file.id}
          label={file.filename}
          image={file.kind === 'image' || file.view === 'image'}
          note="made by the bot"
          link={file.link}
        />
      ))}
    </div>
  )
}

type Reply = { type?: string; text?: string; suggestions?: string[] }

function ReplyBubble({ reply }: { reply: Reply }) {
  return (
    <div className="max-w-[85%] rounded-card border border-stroke bg-surface px-4 py-3">
      {reply.type === 'question' && <div className="mb-1 text-xs font-medium text-amber-600">Question</div>}
      <BotMarkdown text={reply.text ?? ''} />
      {!!reply.suggestions?.length && (
        <div className="mt-1 text-xs text-zinc-500">Suggested: {reply.suggestions.join(' · ')}</div>
      )}
    </div>
  )
}

// Ergo also stores each ChatReply as plain assistant text (for engines that
// replay history); the reply bubble already shows it.
function echoesReply(text: string, replies: string[]): boolean {
  return replies.some(r => text === r || text.startsWith(`${r}\n\nSuggested replies:`))
}

function MessageView({
  message,
  results,
  pending,
  replies,
}: {
  message: Message
  results: Map<string, ToolResult>
  pending: Set<string>
  replies: string[]
}) {
  const [showContext, setShowContext] = useState(false)
  const { hidden, rows } = useContext(FoldContext)
  const user = message.role === 'user'
  const parts = message.blocks.filter(
    b =>
      b.type !== 'tool_result' &&
      !(b.type === 'text' && !user && echoesReply(b.text, replies)) &&
      !(b.type === 'tool_use' && hidden.has(b.id) && !rows.has(b.id)),
  )
  if (!parts.length) return null
  return (
    <div className={`flex flex-col gap-1 ${user ? 'items-end' : 'items-start'}`}>
      {parts.map((block, i) => {
        switch (block.type) {
          case 'text': {
            const from = user ? fromThread(block.text) : null
            // Resume's note to the model (see api/bots.py RESUME_NOTE) reads as a divider, not a message.
            const resumed = user
              ? /^\[Resume\] Your last turn stopped before it finished \((.*?)\)\./.exec(block.text)
              : null
            if (resumed)
              return (
                <div key={i} className="self-center text-xs text-zinc-500" title={block.text}>
                  ↻ Resumed after: {resumed[1]}
                </div>
              )
            if (from?.kind === 'reply')
              // A reply to work this chat sent: one row to open; the card above has its status.
              return (
                <details
                  key={i}
                  className="group w-full max-w-[85%] self-start rounded-card border border-teal/40 bg-teal-tint px-3 py-2"
                >
                  <summary className="flex cursor-pointer list-none items-center gap-2 text-xs text-teal-700 dark:text-teal-300">
                    <span className="transition-transform group-open:rotate-90">▸</span>
                    <span className="font-medium">↩ Reply from {from.who}</span>
                    {from.about && <span className="truncate opacity-80">· re “{from.about}”</span>}
                  </summary>
                  <div className="mt-2 text-sm">
                    <BotMarkdown text={from.body} />
                  </div>
                </details>
              )
            if (from)
              return (
                <div
                  key={i}
                  className="max-w-[85%] self-start rounded-card border border-teal/40 bg-teal-tint px-4 py-3"
                >
                  <div className="mb-1 text-xs font-medium text-teal-700 dark:text-teal-300">
                    ✉ message from {from.who}
                    {from.about && <span className="font-normal text-teal-600/80"> · re “{from.about}”</span>}
                  </div>
                  <BotMarkdown text={from.body} />
                </div>
              )
            return user ? (
              <div
                key={i}
                className="max-w-[85%] rounded-card border border-accent/20 bg-indigo-tint px-4 py-3 text-ink"
              >
                <Markdown text={block.text} />
              </div>
            ) : (
              <div
                key={i}
                className="max-w-[85%] rounded-card border border-stroke bg-surface px-4 py-3 text-sm text-ink"
              >
                <BotMarkdown text={block.text} />
              </div>
            )
          }
          case 'attachment':
            return (
              <div key={i} className="max-w-[85%]">
                <AttachmentView id={block.id} label={block.label} image={block.kind === 'image'} />
              </div>
            )
          case 'thinking':
            return (
              <details key={i} className="max-w-[85%] text-xs text-zinc-500">
                <summary className="cursor-pointer">Thinking</summary>
                <div className="whitespace-pre-wrap">{block.text}</div>
              </details>
            )
          case 'context':
            return (
              <button key={i} className="text-xs text-zinc-400 underline" onClick={() => setShowContext(!showContext)}>
                {showContext ? <pre className="whitespace-pre-wrap text-left">{block.text}</pre> : 'context'}
              </button>
            )
          case 'tool_use':
            if (block.name === REPLY_TOOL) return <ReplyBubble key={i} reply={block.input as Reply} />
            return (
              <Fragment key={i}>
                {rows.has(block.id) && (
                  <div className="w-full max-w-[85%]">
                    <FoldToggle row={rows.get(block.id)!} />
                  </div>
                )}
                {!hidden.has(block.id) && (
                  <div className="w-full max-w-[85%]">
                    <ToolCall use={block} result={results.get(block.id)} pending={pending.has(block.id)} />
                  </div>
                )}
              </Fragment>
            )
          default:
            return null
        }
      })}
    </div>
  )
}

// Images a tool returned (renders, screenshots), shown under its card rather than inside it.
function ToolImages({ result }: { result?: ToolResult }) {
  const refs = imageRefs(result?.content)
  if (!refs.length) return null
  return (
    <div className="mt-1 flex flex-wrap gap-2">
      {refs.map((ref, i) => (
        <AttachmentView key={ref.attachment_id || i} id={ref.attachment_id} label={ref.name || 'image'} image />
      ))}
    </div>
  )
}

// A message from another bot thread starts with a bracketed header
// (see django_ergo.bots.messaging); show it as coming from that thread.
const THREAD_HEADER =
  /^\[(Message|Reply) from (.+?) \(thread [0-9a-f-]+\)(?:\. [^\]]*| to your message: “([^”]*)”)\]\n\n([\s\S]*)$/

function fromThread(text: string) {
  const match = THREAD_HEADER.exec(text)
  if (!match) return null
  return { kind: match[1] === 'Reply' ? 'reply' : 'message', who: match[2], about: match[3] ?? '', body: match[4] }
}

function CallHeader({ call }: { call: Call }) {
  const [detail, setDetail] = useState<unknown>(null)
  const tokens = call.input_tokens + call.output_tokens
  const tone =
    call.status === 'completed'
      ? 'text-emerald-600'
      : call.status === 'awaiting_approval'
        ? 'text-amber-600'
        : call.status === 'stopped'
          ? 'text-zinc-600 dark:text-zinc-400'
          : 'text-red-600'
  return (
    <div className="my-2 text-xs text-zinc-500">
      <button
        className="flex w-full items-center gap-2 hover:text-zinc-800 dark:hover:text-zinc-200"
        onClick={async () => setDetail(detail ? null : await api.call(call.id))}
      >
        <span className="h-px flex-1 bg-zinc-200 dark:bg-zinc-800" />
        <span className="font-mono">{call.kind}</span>
        <span className={tone}>{call.status.replace('_', ' ')}</span>
        <span>{tokens.toLocaleString()} tokens</span>
        {call.model_name && <span>{call.model_name}</span>}
        <span className="h-px flex-1 bg-zinc-200 dark:bg-zinc-800" />
      </button>
      {call.error && (
        <div
          title={call.error_summary ? call.error : undefined}
          className={`mt-1 text-center ${call.status === 'stopped' ? 'text-zinc-500' : 'text-red-600'}`}
        >
          {call.status === 'stopped' ? `⏹ ${call.error}` : call.error_summary || call.error}
        </div>
      )}
      {detail != null && !!call.tools?.length && (
        <div className="mt-2 flex flex-wrap items-center gap-1">
          <span className="mr-1">Tools available:</span>
          {call.tools.map(name => (
            <span key={name} className="rounded bg-zinc-100 px-1.5 py-0.5 font-mono text-[11px] dark:bg-zinc-800">
              {name}
            </span>
          ))}
        </div>
      )}
      {detail != null && (
        <pre className="mt-2 max-h-96 overflow-auto rounded-lg bg-zinc-50 p-3 font-mono text-[11px] dark:bg-zinc-900">
          {pretty(detail)}
        </pre>
      )}
    </div>
  )
}

export function Transcript({
  messages,
  calls,
  files = [],
  complete = true,
  sent = [],
  workers = [],
}: {
  messages: Message[]
  calls: Call[]
  files?: AttachmentFile[]
  complete?: boolean // every message is loaded (no older page)
  sent?: SentCard[] // requests this chat sent (thread cards)
  workers?: Worker[]
}) {
  const { sessions } = useContext(DirectoryContext)
  const [openFolds, setOpenFolds] = useState<Set<string>>(new Set())
  const results = new Map<string, ToolResult>()
  for (const message of messages)
    for (const block of message.blocks) if (block.type === 'tool_result') results.set(block.tool_use_id, block)
  const pending = new Set(
    calls.flatMap(c => (c.status === 'awaiting_approval' ? c.pending_approvals.map(a => a.id) : [])),
  )
  const replies = messages.flatMap(m =>
    m.blocks.flatMap(b =>
      b.type === 'tool_use' && b.name === REPLY_TOOL ? [String((b.input as Reply).text ?? '')] : [],
    ),
  )
  const starts = new Map<number, Call>()
  for (const call of calls) if (call.first_sequence != null) starts.set(call.first_sequence, call)
  const made = placeFiles(messages, files, complete)
  const delegated: Delegated = {
    cards: new Map(sent.map(c => [c.message_id, c])),
    workers: new Map(workers.map(w => [w.id, w])),
    known: new Map([
      ...sessions.map(s => [s.id, s.title] as const),
      ...sent.map(c => [c.thread.id, c.thread.title] as const),
    ]),
  }
  const sentFrom = sentNotes(messages, calls, results, delegated.cards)
  const folds: Folds = {
    ...foldRuns(messages, results, pending, starts, replies, openFolds),
    durations: toolDurations(messages),
    toggle: key =>
      setOpenFolds(open => {
        const next = new Set(open)
        if (next.has(key)) next.delete(key)
        else next.add(key)
        return next
      }),
  }
  return (
    <DelegatedContext.Provider value={delegated}>
      <FoldContext.Provider value={folds}>
        <div className="flex flex-col">
          {made.has(-1) && <BotFiles files={made.get(-1)!} />}
          {messages.map(message => (
            <div
              key={message.line}
              className={`empty:hidden ${toolsOnly(message) && !starts.has(message.line) ? 'mt-1.5' : 'mt-6'}`}
            >
              {starts.has(message.line) && <CallHeader call={starts.get(message.line)!} />}
              <MessageView message={message} results={results} pending={pending} replies={replies} />
              {sentFrom.has(message.line) && (
                <div className="mt-1 flex flex-wrap items-center justify-end gap-1.5 text-xs text-muted">
                  ↪ Sent to
                  {sentFrom.get(message.line)!.map(card => (
                    <ThreadLink key={card.message_id} id={card.thread.id}>
                      {card.thread.title}
                    </ThreadLink>
                  ))}
                </div>
              )}
              {made.has(message.line) && <BotFiles files={made.get(message.line)!} />}
            </div>
          ))}
        </div>
      </FoldContext.Provider>
    </DelegatedContext.Provider>
  )
}

/** A message that only carries tool calls and results: it sits close to the calls around it. */
function toolsOnly(message: Message): boolean {
  return message.blocks.every(
    b =>
      b.type === 'tool_result' ||
      b.type === 'thinking' ||
      b.type === 'context' ||
      (b.type === 'tool_use' && b.name !== REPLY_TOOL),
  )
}

/** The requests each turn sent on, keyed by the line of the user message that started it. */
function sentNotes(
  messages: Message[],
  calls: Call[],
  results: Map<string, ToolResult>,
  cards: Map<string, SentCard>,
): Map<number, SentCard[]> {
  const notes = new Map<number, SentCard[]>()
  for (const message of messages)
    for (const block of message.blocks) {
      if (block.type !== 'tool_use' || !THREAD_TOOLS.has(block.name)) continue
      const card = cards.get(String(resultJson(results.get(block.id))?.message_id ?? ''))
      const call = calls.find(
        c =>
          c.first_sequence != null && c.first_sequence <= message.line && (c.last_sequence ?? Infinity) >= message.line,
      )
      const start = call && messages.find(m => m.line === call.first_sequence && m.role === 'user')
      if (!card || !start) continue
      notes.set(start.line, [...(notes.get(start.line) ?? []), card])
    }
  return notes
}
