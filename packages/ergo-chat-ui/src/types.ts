import type { ReactNode } from 'react'

/** A file the host has already stored. The package only displays it; the host owns upload and authorization. */
export type Attachment = {
  /** Stable id, used by `onRemoveAttachment` and as a React key. */
  id: string
  name: string
  mediaType?: string
  /** Where the browser can load the file (http(s), blob:, a same-origin path or a data:image URL). Unsafe URLs are not rendered as links. */
  url?: string
  /** Bytes, shown next to the name when present. */
  size?: number
}

export type TextPart = { type: 'text'; text: string }

export type AttachmentPart = { type: 'attachment'; attachment: Attachment }

export type ToolStatus = 'running' | 'done' | 'error'

/** A tool call as the host reports it. The package never runs tools. */
export type ToolPart = {
  type: 'tool'
  /** Stable call id (also what an approval card should reference). */
  id: string
  name: string
  status: ToolStatus
  /** Arguments: an object, a JSON string or plain text. */
  input?: unknown
  /** Output (or the error text when status is `error`): a string, an array of `{text}` blocks, or any JSON. */
  result?: unknown
}

/**
 * Host-defined content: approvals, rich cards, anything the package cannot know about.
 * Without a `renderPart` that handles it, it renders as a small neutral placeholder.
 */
export type CustomPart = { type: 'custom'; kind: string; id?: string; data?: unknown }

export type Part = TextPart | AttachmentPart | ToolPart | CustomPart

export type Role = 'user' | 'assistant' | 'system'

export type Message = {
  /** Stable and unique per message: the host dedupes replayed events before passing messages in. */
  id: string
  role: Role
  parts: Part[]
  /** ISO 8601. Shown as a tooltip on the message. */
  createdAt?: string
}

/** `sending`: a send is in flight. `streaming`: the host is receiving the assistant's turn. */
export type ChatStatus = 'idle' | 'sending' | 'streaming' | 'error'

export type PartContext = {
  message: Message
  /** Index of the part in `message.parts`. */
  index: number
  /** What the package would render, so a host can wrap it. */
  defaultRender: ReactNode
}

/**
 * Return a node to replace the default rendering (`null` renders nothing),
 * or `undefined` to keep the default. Receives parts of any `type` the host sent, even ones this version does not know.
 */
export type RenderPart = (part: Part, context: PartContext) => ReactNode | undefined

export type ChatTheme = 'light' | 'dark' | 'auto'

export type ComposerProps = {
  /** Called with the trimmed text and the pending attachments. Reject to keep the draft and show an error. */
  onSend: (text: string, attachments: Attachment[]) => void | Promise<void>
  /** Shown instead of the send button while `busy`. */
  onStop?: () => void
  /** Called with the chosen files. Resolve with the stored attachments. The attach button is hidden without it. */
  onAttach?: (files: File[]) => Promise<Attachment[]>
  /** Called before a pending attachment is dropped. Reject to keep it. */
  onRemoveAttachment?: (attachment: Attachment) => void | Promise<void>
  /** True while a send is in flight or the assistant is streaming. */
  busy?: boolean
  disabled?: boolean
  placeholder?: string
  /** Controlled draft text. Omit for an uncontrolled composer. */
  draft?: string
  onDraftChange?: (text: string) => void
  /** Controlled pending attachments. Omit for an uncontrolled composer. */
  attachments?: Attachment[]
  onAttachmentsChange?: (attachments: Attachment[]) => void
  /** `accept` for the file input. */
  accept?: string
  theme?: ChatTheme
  className?: string
}

export type TranscriptProps = {
  messages: Message[]
  renderPart?: RenderPart
  /** Shown when there are no messages. */
  empty?: ReactNode
  /** Shown after the last message, e.g. a typing indicator. */
  footer?: ReactNode
  /** Render markdown images from http(s) URLs. Off by default: remote images from untrusted text can track readers. */
  allowMarkdownImages?: boolean
  theme?: ChatTheme
  className?: string
}

export type ChatViewProps = Omit<TranscriptProps, 'className'> &
  Pick<
    ComposerProps,
    'onSend' | 'onStop' | 'onAttach' | 'onRemoveAttachment' | 'placeholder' | 'accept' | 'draft' | 'onDraftChange'
  > & {
    status?: ChatStatus
    /** Text shown in an alert above the composer when `status` is `error`. */
    error?: ReactNode
    /** Replace the composer entirely (keeps the transcript). */
    composer?: ReactNode
    className?: string
  }
