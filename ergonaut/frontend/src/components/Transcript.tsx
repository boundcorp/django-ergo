import { useState } from 'react'
import type { Block, Call, Message } from '../api'
import { api } from '../api'
import { ToolCard, pretty } from './ToolCard'

type ToolResult = Extract<Block, { type: 'tool_result' }>

const REPLY_TOOL = 'send_reply'

type Reply = { type?: string; text?: string; suggestions?: string[] }

function ReplyBubble({ reply }: { reply: Reply }) {
  return (
    <div className="max-w-[85%] rounded-2xl rounded-tl-sm bg-zinc-100 px-4 py-2 dark:bg-zinc-800">
      {reply.type === 'question' && <div className="mb-1 text-xs font-medium text-amber-600">Question</div>}
      <div className="whitespace-pre-wrap">{reply.text}</div>
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
          case 'text':
            return user ? (
              <div key={i} className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-tr-sm bg-indigo-600 px-4 py-2 text-white">
                {block.text}
              </div>
            ) : (
              <div key={i} className="max-w-[85%] whitespace-pre-wrap text-sm text-zinc-500 italic">{block.text}</div>
            )
          case 'attachment':
            return (
              <div key={i} className="rounded-md border border-zinc-300 px-2 py-1 text-xs text-zinc-500 dark:border-zinc-700">
                📎 {block.label}
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
              </div>
            )
          default:
            return null
        }
      })}
    </div>
  )
}

function CallHeader({ call }: { call: Call }) {
  const [detail, setDetail] = useState<unknown>(null)
  const tokens = call.input_tokens + call.output_tokens
  const tone =
    call.status === 'completed' ? 'text-emerald-600' : call.status === 'awaiting_approval' ? 'text-amber-600' : 'text-red-600'
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
      {call.error && <div className="mt-1 text-center text-red-600">{call.error}</div>}
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

export function Transcript({ messages, calls }: { messages: Message[]; calls: Call[] }) {
  const results = new Map<string, ToolResult>()
  for (const message of messages)
    for (const block of message.blocks) if (block.type === 'tool_result') results.set(block.tool_use_id, block)
  const pending = new Set(calls.flatMap(c => (c.status === 'awaiting_approval' ? c.pending_approvals.map(a => a.id) : [])))
  const replies = messages.flatMap(m =>
    m.blocks.flatMap(b => (b.type === 'tool_use' && b.name === REPLY_TOOL ? [String((b.input as Reply).text ?? '')] : [])),
  )
  const starts = new Map<number, Call>()
  for (const call of calls) if (call.first_sequence != null) starts.set(call.first_sequence, call)
  return (
    <div className="flex flex-col gap-3">
      {messages.map(message => (
        <div key={message.line}>
          {starts.has(message.line) && <CallHeader call={starts.get(message.line)!} />}
          <MessageView message={message} results={results} pending={pending} replies={replies} />
        </div>
      ))}
    </div>
  )
}
