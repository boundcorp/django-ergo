import { useId, useRef, useState, type KeyboardEvent } from 'react'
import { AttachmentView } from './Attachments'
import { useRoot } from './root'
import type { Attachment, ComposerProps } from './types'

/** Controlled when `value` is given, otherwise keeps its own state. */
function useControllable<T>(value: T | undefined, onChange: ((v: T) => void) | undefined, initial: T) {
  const [own, setOwn] = useState(initial)
  const controlled = value !== undefined
  const current = controlled ? value : own
  return [
    current,
    (next: T) => {
      if (!controlled) setOwn(next)
      onChange?.(next)
    },
  ] as const
}

function errorText(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback
}

/**
 * Text box, attach button and send/stop. It never uploads or sends anything itself:
 * `onAttach` and `onSend` are the host's. A rejected `onSend` keeps the draft and attachments so the user can retry.
 */
export function Composer({
  onSend,
  onStop,
  onAttach,
  onRemoveAttachment,
  busy = false,
  disabled = false,
  placeholder = 'Message',
  draft,
  onDraftChange,
  attachments,
  onAttachmentsChange,
  accept,
  theme = 'light',
  className = '',
}: ComposerProps) {
  const root = useRoot(theme, 'ergo-chat__composer', className)
  const [text, setText] = useControllable(draft, onDraftChange, '')
  const [files, setFiles] = useControllable<Attachment[]>(attachments, onAttachmentsChange, [])
  const [uploading, setUploading] = useState(0)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const input = useRef<HTMLInputElement>(null)
  const errorId = useId()
  // Local state can lag a controlled prop by a render; refs keep the latest value for async handlers.
  const filesRef = useRef(files)
  filesRef.current = files

  const trimmed = text.trim()
  const canSend = !disabled && !sending && !uploading && (trimmed.length > 0 || files.length > 0)

  async function send() {
    if (!canSend) return
    setError(null)
    setSending(true)
    try {
      await onSend(trimmed, files)
      setText('')
      setFiles([])
    } catch (e) {
      setError(errorText(e, 'Could not send. Your message is still here.'))
    } finally {
      setSending(false)
    }
  }

  async function attach(list: FileList | null) {
    const chosen = list ? Array.from(list) : []
    if (input.current) input.current.value = ''
    if (!onAttach || !chosen.length) return
    setError(null)
    setUploading(n => n + 1)
    try {
      const stored = await onAttach(chosen)
      setFiles([...filesRef.current, ...stored])
    } catch (e) {
      setError(errorText(e, 'Could not attach the file.'))
    } finally {
      setUploading(n => n - 1)
    }
  }

  async function remove(attachment: Attachment) {
    setError(null)
    try {
      await onRemoveAttachment?.(attachment)
      setFiles(filesRef.current.filter(f => f.id !== attachment.id))
    } catch (e) {
      setError(errorText(e, 'Could not remove the file.'))
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends; Shift+Enter is a newline; Enter that confirms an IME candidate is not a send.
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      void send()
    }
  }

  return (
    <form
      {...root}
      onSubmit={event => {
        event.preventDefault()
        void send()
      }}
    >
      {files.length > 0 && (
        <ul className="ergo-chat__pending" aria-label="Attachments">
          {files.map(file => (
            <li key={file.id} className="ergo-chat__pending-item">
              <AttachmentView attachment={file} />
              <button
                type="button"
                className="ergo-chat__btn ergo-chat__btn--small"
                aria-label={`Remove ${file.name}`}
                onClick={() => void remove(file)}
              >
                ✕
              </button>
            </li>
          ))}
        </ul>
      )}
      {uploading > 0 && (
        <div className="ergo-chat__muted" role="status">
          Uploading…
        </div>
      )}
      {error && (
        <div className="ergo-chat__alert" role="alert" id={errorId}>
          {error}
        </div>
      )}
      <div className="ergo-chat__composer-row">
        {onAttach && (
          <>
            <input
              ref={input}
              type="file"
              multiple
              accept={accept}
              className="ergo-chat__sr"
              tabIndex={-1}
              aria-hidden
              onChange={event => void attach(event.target.files)}
            />
            <button
              type="button"
              className="ergo-chat__btn"
              aria-label="Attach files"
              disabled={disabled}
              onClick={() => input.current?.click()}
            >
              📎
            </button>
          </>
        )}
        <textarea
          className="ergo-chat__input"
          rows={1}
          value={text}
          placeholder={placeholder}
          aria-label="Message"
          aria-describedby={error ? errorId : undefined}
          disabled={disabled}
          onChange={event => setText(event.target.value)}
          onKeyDown={onKeyDown}
        />
        {busy && onStop ? (
          <button type="button" className="ergo-chat__btn ergo-chat__btn--primary" onClick={onStop}>
            Stop
          </button>
        ) : (
          <button type="submit" className="ergo-chat__btn ergo-chat__btn--primary" disabled={!canSend}>
            Send
          </button>
        )}
      </div>
    </form>
  )
}
