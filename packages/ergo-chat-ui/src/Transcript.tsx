import { Fragment, useEffect, useLayoutEffect, useRef, type ReactNode } from 'react'
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
  index,
  renderPart,
  renderMessage,
  allowImages,
}: {
  message: Message
  index: number
  renderPart?: TranscriptProps['renderPart']
  renderMessage?: TranscriptProps['renderMessage']
  allowImages: boolean
}) {
  const parts = Array.isArray(message.parts) ? message.parts : []
  const label = message.role === 'user' ? 'You' : message.role === 'system' ? 'System' : 'Assistant'
  const nodes: { key: string; node: ReactNode }[] = []
  parts.forEach((part, i) => {
    const fallback = defaultPart(part, allowImages)
    const custom = renderPart?.(part, { message, index: i, defaultRender: fallback })
    const node = custom === undefined ? fallback : custom
    if (node !== null) nodes.push({ key: partKey(part, i), node })
  })
  const article = (
    <article className={`ergo-chat__msg ergo-chat__msg--${message.role}`} data-message-id={message.id}>
      <header className="ergo-chat__msg-head">
        <span className="ergo-chat__sr">{label}</span>
      </header>
      <div className="ergo-chat__bubble" title={message.createdAt}>
        {nodes.map(({ key, node }) => (
          <div className="ergo-chat__part" key={key}>
            {node}
          </div>
        ))}
      </div>
    </article>
  )
  if (!renderMessage) return article
  const custom = renderMessage(message, {
    index,
    parts: nodes.map(({ key, node }) => <Fragment key={key}>{node}</Fragment>),
    defaultRender: article,
  })
  return custom === undefined ? article : custom
}

// useLayoutEffect warns during SSR in React 18; this is the usual guard.
const useIsoLayoutEffect = typeof window === 'undefined' ? useEffect : useLayoutEffect

/** The message list. Scrolls to the newest message while the reader is at the bottom, and leaves them alone once they scroll up. */
export function Transcript({
  messages,
  renderPart,
  renderMessage,
  header,
  scroll = 'pin',
  empty,
  footer,
  allowMarkdownImages = false,
  theme = 'light',
  className = '',
}: TranscriptProps) {
  const scroller = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)
  const host = scroll === 'host'
  const root = useRoot(theme, host ? 'ergo-chat--bare ergo-chat__list' : 'ergo-chat__transcript', className)
  useIsoLayoutEffect(() => {
    const el = scroller.current
    if (el && pinned.current && !host) el.scrollTop = el.scrollHeight
  })
  const body = (
    <>
      {header}
      {messages.length === 0 && empty ? <div className="ergo-chat__empty">{empty}</div> : null}
      {messages.map((message, index) => (
        <MessageView
          key={message.id}
          message={message}
          index={index}
          renderPart={renderPart}
          renderMessage={renderMessage}
          allowImages={allowMarkdownImages}
        />
      ))}
      {footer}
    </>
  )
  if (host) return <div {...root}>{body}</div>
  return (
    <div
      ref={scroller}
      {...root}
      role="log"
      aria-live="polite"
      aria-label="Conversation"
      tabIndex={0}
      onScroll={event => {
        const el = event.currentTarget
        pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48
      }}
    >
      {body}
    </div>
  )
}
