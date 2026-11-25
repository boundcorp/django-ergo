import { useId, useRef, useState, type ClipboardEvent, type KeyboardEvent } from 'react'
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
  textareaRef,
  canSend: canSendProp,
  clearOnSend = true,
  sendWhileBusy = false,
  stopDisabled = false,
  attachDisabled = false,
  renderPending,
  afterInput,
  actions,
  footer,
  buttons,
  titles,
  icons,
  classNames,
  unstyled = false,
}: ComposerProps) {
  const cx = (...names: (string | undefined | false)[]) => names.filter(Boolean).join(' ')
  // `unstyled` keeps the scoped root (tokens, theme) but none of the package's layout or visual classes.
  const own = (name: string | undefined) => (unstyled ? undefined : name)
  const root = useRoot(theme, cx(unstyled ? 'ergo-chat--bare' : 'ergo-chat__composer', classNames?.root), className)
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
  const computed = !disabled && !sending && !uploading && (trimmed.length > 0 || files.length > 0)
  // A host-owned guard (`canSend`) governs the button; Enter and submit then reach `onSend`, where the host explains a refusal.
  const canSend = canSendProp ?? computed
  const mayAttempt = canSendProp === undefined ? computed : !disabled && !sending

  async function send() {
    if (!mayAttempt) return
    setError(null)
    setSending(true)
    try {
      await onSend(trimmed, files)
      if (clearOnSend) {
        setText('')
        setFiles([])
      }
    } catch (e) {
      setError(errorText(e, 'Could not send. Your message is still here.'))
    } finally {
      setSending(false)
    }
  }

  async function attach(list: FileList | null) {
    const chosen = list ? Array.from(list) : []
    if (input.current) input.current.value = ''
    await attachFiles(chosen)
  }

  async function attachFiles(chosen: File[]) {
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

  function onPaste(event: ClipboardEvent<HTMLTextAreaElement>) {
    // Pasted files (a screenshot) go through the host's upload; pasted text is untouched.
    if (!onAttach || disabled || attachDisabled) return
    const pasted = Array.from(event.clipboardData?.files ?? [])
    if (!pasted.length) return
    event.preventDefault()
    void attachFiles(pasted)
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
        <ul className={cx(own('ergo-chat__pending'), classNames?.pending)} aria-label="Attachments">
          {files.map(file =>
            renderPending ? (
              <li key={file.id}>{renderPending(file, () => void remove(file))}</li>
            ) : (
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
            ),
          )}
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
      <div className={cx(own('ergo-chat__composer-row'), classNames?.row)}>
        {onAttach && (
          <>
            <input
              ref={input}
              type="file"
              multiple
              accept={accept}
              className={unstyled ? undefined : 'ergo-chat__sr'}
              hidden={unstyled}
              tabIndex={-1}
              aria-hidden
              onChange={event => void attach(event.target.files)}
            />
            <button
              type="button"
              className={cx(own('ergo-chat__btn'), classNames?.attach)}
              aria-label={buttons?.attach ?? 'Attach files'}
              title={titles?.attach}
              disabled={disabled || attachDisabled}
              onClick={() => input.current?.click()}
            >
              {icons?.attach ?? '📎'}
            </button>
          </>
        )}
        <textarea
          ref={textareaRef}
          className={cx(own('ergo-chat__input'), classNames?.input)}
          rows={1}
          value={text}
          placeholder={placeholder}
          aria-label="Message"
          aria-describedby={error ? errorId : undefined}
          disabled={disabled}
          onChange={event => setText(event.target.value)}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
        />
        {afterInput}
        {busy && onStop && (
          <button
            type="button"
            className={cx(
              own('ergo-chat__btn'),
              own(sendWhileBusy ? undefined : 'ergo-chat__btn--primary'),
              classNames?.stop,
            )}
            aria-label={buttons?.stop}
            title={titles?.stop}
            disabled={stopDisabled}
            onClick={onStop}
          >
            {icons?.stop ?? 'Stop'}
          </button>
        )}
        {actions}
        {(!busy || !onStop || sendWhileBusy) && (
          <button
            type="submit"
            className={cx(own('ergo-chat__btn ergo-chat__btn--primary'), classNames?.send)}
            aria-label={buttons?.send}
            title={titles?.send}
            disabled={!canSend}
          >
            {icons?.send ?? 'Send'}
          </button>
        )}
      </div>
      {footer}
    </form>
  )
}
