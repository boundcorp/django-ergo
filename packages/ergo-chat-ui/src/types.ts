import type { ReactNode, Ref } from 'react'

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
  /** Host data carried along untouched (never rendered): the adapter's own record, so `renderPart`/`renderMessage` can read it back. */
  meta?: unknown
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

/** What `renderMessage` receives besides the message. */
export type MessageContext = {
  /** Index in `messages`. */
  index: number
  /** The message's parts, each already run through `renderPart` (or the default); hidden parts are left out. Keyed. */
  parts: ReactNode[]
  /** The package's own article for the message (header, bubble, parts), so a host can wrap it. */
  defaultRender: ReactNode
}

/**
 * Message-level slot. Return a node to replace the whole message (the host then lays out `ctx.parts` itself, adds its
 * own labels, cards before or after, anchors), `null` to hide the message, or `undefined` to keep the default.
 */
export type RenderMessage = (message: Message, context: MessageContext) => ReactNode | undefined

export type ChatTheme = 'light' | 'dark' | 'auto'

/** Class names for the composer's elements, added to the package's own (or used alone with `unstyled`). */
export type ComposerClassNames = Partial<
  Record<'root' | 'row' | 'input' | 'attach' | 'send' | 'stop' | 'pending', string>
>

/** Accessible names / tooltips (`ComposerButtons`) and visible content (`ComposerIcons`) of the composer's buttons. */
export type ComposerButtons = Partial<Record<'send' | 'stop' | 'attach', string>>
export type ComposerIcons = Partial<Record<'send' | 'stop' | 'attach', ReactNode>>

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

  // Host integration. All optional; without them the composer behaves exactly as above.
  /** Ref to the textarea (focus, selection, auto-resize and voice insertion stay the host's). */
  textareaRef?: Ref<HTMLTextAreaElement>
  /**
   * Host-owned send guard. When given it replaces the computed rule for the Send button, and Enter/submit call `onSend`
   * whenever the composer is not `disabled` and not composing (the host decides, and may explain, why not). When
   * omitted the package guards both: text or attachments present, not sending, not uploading.
   */
  canSend?: boolean
  /** Default true: a resolved `onSend` clears the draft and attachments. Pass false when the host clears them itself (e.g. only if the text is unchanged). */
  clearOnSend?: boolean
  /** Keep the Send button next to Stop while `busy` (steering a running turn) instead of replacing it. Default false. */
  sendWhileBusy?: boolean
  /** Disables only the attach button (the host is uploading, or attachments are not allowed right now). */
  attachDisabled?: boolean
  /** Disables the Stop button (e.g. a stop is already requested). */
  stopDisabled?: boolean
  /** Custom pending-attachment chip. Call `remove()` for the same path as the package's own ✕. */
  renderPending?: (attachment: Attachment, remove: () => void) => ReactNode
  /** Rendered after the textarea, before the Stop/Send buttons (a voice button). */
  afterInput?: ReactNode
  /** Rendered between Stop and Send (e.g. "Stop & send"). */
  actions?: ReactNode
  /** Rendered under the input row inside the composer (e.g. a model link). */
  footer?: ReactNode
  /** Accessible names for the buttons. */
  buttons?: ComposerButtons
  /** Tooltips for the buttons. */
  titles?: ComposerButtons
  /** Visible content of the buttons (an icon, say). */
  icons?: ComposerIcons
  classNames?: ComposerClassNames
  /** Drop the package's own layout and visual classes (keeps the scoped root tokens); the host styles via `classNames`. */
  unstyled?: boolean
}

export type TranscriptProps = {
  messages: Message[]
  renderPart?: RenderPart
  /** Shown when there are no messages. */
  empty?: ReactNode
  /** Message-level slot, see `RenderMessage`. */
  renderMessage?: RenderMessage
  /** Shown before the first message. */
  header?: ReactNode
  /**
   * `pin` (default): the transcript is the scroller and follows new messages while the reader is at the bottom.
   * `host`: a plain, non-scrolling list; the host owns the scroll container, pinning, jump-to-bottom and anchors.
   */
  scroll?: 'pin' | 'host'
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
