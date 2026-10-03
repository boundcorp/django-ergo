import { useState } from 'react'
import type { AttachmentFile, Block, Call, Message } from '../api'
import { api } from '../api'
import Markdown from './Markdown'
import { ToolCard, pretty } from './ToolCard'

type ToolResult = Extract<Block, { type: 'tool_result' }>

const REPLY_TOOL = 'send_reply'

// A file in the chat: images as a thumbnail, anything else as a chip. Both open the file.
export function AttachmentView({
  id,
  label,
  image,
  note,
}: {
  id?: string
  label: string
  image?: boolean
  note?: string
}) {
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
function placeFiles(messages: Message[], files: AttachmentFile[]): Map<number, AttachmentFile[]> {
  const shown = shownFileIds(messages)
  const placed = new Map<number, AttachmentFile[]>()
  const stamped = messages.filter(m => m.timestamp)
  for (const file of files) {
    if (file.source !== 'bot' || file.message_sequence != null || file.archived_at || shown.has(file.id)) continue
    const created = Date.parse(file.created_at)
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
      <Markdown text={reply.text ?? ''} />
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
  const user = message.role === 'user'
  const parts = message.blocks.filter(
    b => b.type !== 'tool_result' && !(b.type === 'text' && !user && echoesReply(b.text, replies)),
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
            if (from)
              return (
                <div
                  key={i}
                  className="max-w-[85%] self-start rounded-card border border-teal/40 bg-teal-tint px-4 py-3"
                >
                  <div className="mb-1 text-xs font-medium text-teal-700 dark:text-teal-300">
                    {from.kind === 'reply' ? '↩ reply from' : '✉ message from'} {from.who}
                    {from.about && <span className="font-normal text-teal-600/80"> · re “{from.about}”</span>}
                  </div>
                  <Markdown text={from.body} />
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
                <Markdown text={block.text} />
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
              <div key={i} className="w-full max-w-[85%]">
                <ToolCard use={block} result={results.get(block.id)} pending={pending.has(block.id)} />
                <ToolImages result={results.get(block.id)} />
              </div>
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
}: {
  messages: Message[]
  calls: Call[]
  files?: AttachmentFile[]
}) {
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
  const made = placeFiles(messages, files)
  return (
    <div className="flex flex-col gap-6">
      {made.has(-1) && <BotFiles files={made.get(-1)!} />}
      {messages.map(message => (
        <div key={message.line}>
          {starts.has(message.line) && <CallHeader call={starts.get(message.line)!} />}
          <MessageView message={message} results={results} pending={pending} replies={replies} />
          {made.has(message.line) && <BotFiles files={made.get(message.line)!} />}
        </div>
      ))}
    </div>
  )
}
