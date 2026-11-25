import { useEffect, useLayoutEffect, useRef, type ReactNode } from 'react'
import { AttachmentList } from './Attachments'
import { Markdown } from './Markdown'
import { useRoot } from './root'
import { ToolCard } from './ToolCard'
import type { Message, Part, TranscriptProps } from './types'

function Fallback({ label }: { label: string }) {
  return <div className="ergo-chat__unknown">{label}</div>
}

/** The package's own rendering of a part. Unknown or malformed parts become a neutral placeholder; nothing is evaluated. */
function defaultPart(part: Part, allowImages: boolean): ReactNode {
  const p = part as { type?: unknown } & Record<string, unknown>
  switch (p.type) {
    case 'text':
      return typeof p.text === 'string' && p.text ? <Markdown text={p.text} allowImages={allowImages} /> : null
    case 'attachment':
      return p.attachment && typeof p.attachment === 'object' ? (
        <AttachmentList attachments={[(part as Extract<Part, { type: 'attachment' }>).attachment]} />
      ) : null
    case 'tool':
      return typeof p.id === 'string' && typeof p.name === 'string' ? (
        <ToolCard part={part as Extract<Part, { type: 'tool' }>} />
      ) : (
        <Fallback label="Unsupported tool call" />
      )
    case 'custom':
      return <Fallback label={`Unsupported content: ${String(p.kind ?? 'custom')}`} />
    default:
      return <Fallback label={`Unsupported content: ${typeof p.type === 'string' ? p.type : 'unknown'}`} />
  }
}

function partKey(part: Part, index: number): string {
  const p = part as { type?: unknown; id?: unknown; attachment?: { id?: unknown } }
  const id = p.type === 'attachment' ? p.attachment?.id : p.id
  return typeof id === 'string' ? `${String(p.type)}:${id}` : `${index}`
}

function MessageView({
  message,
  renderPart,
  allowImages,
}: {
  message: Message
  renderPart?: TranscriptProps['renderPart']
  allowImages: boolean
}) {
  const parts = Array.isArray(message.parts) ? message.parts : []
  const label = message.role === 'user' ? 'You' : message.role === 'system' ? 'System' : 'Assistant'
  return (
    <article className={`ergo-chat__msg ergo-chat__msg--${message.role}`} data-message-id={message.id}>
      <header className="ergo-chat__msg-head">
        <span className="ergo-chat__sr">{label}</span>
      </header>
      <div className="ergo-chat__bubble" title={message.createdAt}>
        {parts.map((part, index) => {
          const fallback = defaultPart(part, allowImages)
          const custom = renderPart?.(part, { message, index, defaultRender: fallback })
          const node = custom === undefined ? fallback : custom
          return node === null ? null : (
            <div className="ergo-chat__part" key={partKey(part, index)}>
              {node}
            </div>
          )
        })}
      </div>
    </article>
  )
}

// useLayoutEffect warns during SSR in React 18; this is the usual guard.
const useIsoLayoutEffect = typeof window === 'undefined' ? useEffect : useLayoutEffect

/** The message list. Scrolls to the newest message while the reader is at the bottom, and leaves them alone once they scroll up. */
export function Transcript({
  messages,
  renderPart,
  empty,
  footer,
  allowMarkdownImages = false,
  theme = 'light',
  className = '',
}: TranscriptProps) {
  const scroller = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)
  useIsoLayoutEffect(() => {
    const el = scroller.current
    if (el && pinned.current) el.scrollTop = el.scrollHeight
  })
  return (
    <div
      ref={scroller}
      {...useRoot(theme, 'ergo-chat__transcript', className)}
      role="log"
      aria-live="polite"
      aria-label="Conversation"
      tabIndex={0}
      onScroll={event => {
        const el = event.currentTarget
        pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48
      }}
    >
      {messages.length === 0 && empty ? <div className="ergo-chat__empty">{empty}</div> : null}
      {messages.map(message => (
        <MessageView key={message.id} message={message} renderPart={renderPart} allowImages={allowMarkdownImages} />
      ))}
      {footer}
    </div>
  )
}
