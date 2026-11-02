import {
  useEffect,
  useId,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from 'react'
import { QUICK_REPLY_HOLD_MS, QuickReplyHoldController } from '../quickReplyHold'

type SuggestionChipsProps = {
  suggestions: string[]
  disabled?: boolean
  onPick: (text: string) => void
  variant?: 'desktop' | 'mobile'
}

/** Suggested replies require a deliberate hold before their text is sent. */
export function SuggestionChips({ suggestions, disabled, onPick, variant = 'mobile' }: SuggestionChipsProps) {
  const hintId = useId()
  const onPickRef = useRef(onPick)
  const wasDisabled = useRef(!!disabled)
  const [holding, setHolding] = useState<string | null>(null)
  const [sent, setSent] = useState(false)
  const controller = useRef<QuickReplyHoldController | null>(null)
  const suggestionKey = suggestions.join('\u0000')

  onPickRef.current = onPick
  if (!controller.current) {
    controller.current = new QuickReplyHoldController(setHolding, text => {
      setSent(true)
      onPickRef.current(text)
    })
  }
  const hold = controller.current

  useEffect(() => {
    hold.setDisabled(!!disabled)
    if (wasDisabled.current && !disabled) {
      hold.reset()
      setSent(false)
    }
    wasDisabled.current = !!disabled
  }, [disabled, hold])

  useEffect(() => {
    hold.reset()
    setSent(false)
  }, [hold, suggestionKey])

  useEffect(() => () => hold.dispose(), [hold])

  if (!suggestions.length) return null
  const chipClass =
    variant === 'desktop'
      ? 'suggestion-chip quick-reply-chip rounded-full border border-indigo-300 px-3 py-1 text-sm text-indigo-700 hover:bg-indigo-50 disabled:opacity-50 dark:border-indigo-700 dark:text-indigo-300 dark:hover:bg-indigo-950'
      : 'suggestion-chip quick-reply-chip'
  const rowClass =
    variant === 'desktop'
      ? 'suggestion-desktop mx-4 mb-2 flex flex-wrap gap-2 sm:mx-6'
      : 'suggestion-mobile suggestion-row'
  const progressStyle = { '--quick-reply-hold-duration': `${QUICK_REPLY_HOLD_MS}ms` } as CSSProperties

  return (
    <div className={rowClass} role="group" aria-label="Suggested replies">
      <span id={hintId} className="sr-only">
        Hold Enter, Space, or press and hold for 0.7 seconds to send a suggested reply.
      </span>
      {suggestions.map((suggestion, index) => (
        <button
          key={index}
          type="button"
          className={chipClass}
          style={progressStyle}
          disabled={disabled || sent}
          aria-label={`${suggestion}. Hold for 0.7 seconds to send.`}
          aria-describedby={hintId}
          data-holding={holding === suggestion || undefined}
          onClick={event => event.preventDefault()}
          onContextMenu={event => event.preventDefault()}
          onDragStart={event => event.preventDefault()}
          onPointerDown={(event: ReactPointerEvent<HTMLButtonElement>) => {
            if (event.button !== 0 || disabled || sent) return
            hold.beginPointer(suggestion, event.pointerId, event.clientX, event.clientY)
          }}
          onPointerMove={(event: ReactPointerEvent<HTMLButtonElement>) =>
            hold.movePointer(event.pointerId, event.clientX, event.clientY)
          }
          onPointerUp={(event: ReactPointerEvent<HTMLButtonElement>) => hold.cancelPointer(event.pointerId)}
          onPointerCancel={(event: ReactPointerEvent<HTMLButtonElement>) => hold.cancelPointer(event.pointerId)}
          onPointerLeave={() => hold.cancel()}
          onBlur={() => hold.cancel()}
          onKeyDown={(event: ReactKeyboardEvent<HTMLButtonElement>) => {
            if (event.repeat || (event.key !== 'Enter' && event.key !== ' ')) return
            event.preventDefault()
            if (!disabled && !sent) hold.beginKeyboard(suggestion)
          }}
          onKeyUp={(event: ReactKeyboardEvent<HTMLButtonElement>) => {
            if (event.key !== 'Enter' && event.key !== ' ') return
            event.preventDefault()
            hold.endKeyboard()
          }}
        >
          <span className="quick-reply-progress" aria-hidden="true" />
          <span className="quick-reply-label">{suggestion}</span>
        </button>
      ))}
    </div>
  )
}

/** Edit a shared session title without interrupting the rest of the session. */
export function SessionTitle({
  title,
  onSave,
  mobile = false,
}: {
  title: string
  onSave: (title: string) => Promise<void>
  mobile?: boolean
}) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState(title)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const input = useRef<HTMLInputElement>(null)
  const button = useRef<HTMLButtonElement>(null)
  const errorId = useId()

  useEffect(() => {
    if (editing) {
      input.current?.focus()
      input.current?.select()
    }
  }, [editing])

  function cancel() {
    setEditing(false)
    setError('')
    requestAnimationFrame(() => button.current?.focus())
  }

  async function save() {
    const trimmed = value.trim()
    if (!trimmed || Array.from(trimmed).length > 200) {
      setError('Enter a title between 1 and 200 characters.')
      return
    }
    setSaving(true)
    setError('')
    try {
      await onSave(trimmed)
      cancel()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  if (editing) {
    return (
      <form
        className="mt-1 flex min-w-0 flex-wrap items-center gap-2"
        onSubmit={event => {
          event.preventDefault()
          void save()
        }}
        onKeyDown={event => {
          if (event.key === 'Escape') {
            event.preventDefault()
            event.stopPropagation()
            if (!saving) cancel()
          }
        }}
      >
        <label className="sr-only" htmlFor={`${errorId}-input`}>
          Session title
        </label>
        <input
          ref={input}
          id={`${errorId}-input`}
          className="min-w-0 flex-1 basis-40 rounded border border-zinc-400 bg-transparent px-2 py-1 text-base text-ink"
          value={value}
          onChange={event => setValue(event.target.value)}
          aria-invalid={!!error}
          aria-describedby={error ? errorId : undefined}
          disabled={saving}
        />
        <button
          type="submit"
          disabled={saving}
          className="rounded border border-zinc-400 px-2 py-1 text-sm disabled:opacity-50"
        >
          {saving ? 'Saving…' : 'Save'}
        </button>
        <button
          type="button"
          disabled={saving}
          onClick={cancel}
          className="rounded border border-zinc-400 px-2 py-1 text-sm disabled:opacity-50"
        >
          Cancel
        </button>
        {error && (
          <span id={errorId} role="alert" className="w-full text-sm text-red-600">
            {error}
          </span>
        )}
      </form>
    )
  }

  return (
    <div className="flex min-w-0 items-start gap-2">
      {mobile ? (
        <h2 className="chat-sheet-title max-h-24 min-w-0 overflow-y-auto [overflow-wrap:anywhere]">{title}</h2>
      ) : (
        <h1 className="chat-title min-w-0 font-display text-2xl font-bold [overflow-wrap:anywhere]">{title}</h1>
      )}
      <button
        ref={button}
        type="button"
        className="shrink-0 rounded border border-zinc-400 px-2 py-1 text-xs"
        aria-label="Rename session"
        onClick={() => {
          setValue(title)
          setError('')
          setEditing(true)
        }}
      >
        Rename
      </button>
    </div>
  )
}

/**
 * Phone-only session bar. The agent name, title and counts stay compact; the sheet holds
 * the options, meta, actions, waiting list, worker cards and pins. Those children stay
 * mounted, so a live session update refreshes the counts and the open sheet together.
 * The sheet is controlled so other controls (the composer's model link) can open it.
 */
export function MobileSessionBar({
  bot,
  title,
  titleEditor,
  summary,
  open,
  onOpenChange,
  options,
  meta,
  resolved,
  actions,
  children,
}: {
  bot: string
  title: string
  titleEditor: ReactNode
  summary: string
  open: boolean
  onOpenChange: (open: boolean) => void
  options?: ReactNode
  meta: ReactNode
  resolved?: ReactNode
  actions: ReactNode
  children: ReactNode
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const label = summary || 'Session details'

  useEffect(() => {
    const el = dialog.current
    if (!el) return
    if (open && !el.open) el.showModal()
    else if (!open && el.open) el.close()
  }, [open])

  return (
    <>
      <div className="chat-mobile-bar">
        <button
          type="button"
          className="chat-mobile-summary"
          aria-expanded={open}
          aria-controls="chat-session-sheet"
          aria-haspopup="dialog"
          aria-label={`${bot}, ${title}. ${label}. Open session details`}
          data-status-summary={summary}
          onClick={() => onOpenChange(true)}
        >
          <span className="chat-mobile-heading">
            <span className="chat-mobile-bot">{bot}</span>
            <span className="chat-mobile-title">{title}</span>
          </span>
          <span className="chat-mobile-counts">{summary}</span>
        </button>
      </div>
      <dialog
        ref={dialog}
        id="chat-session-sheet"
        className="chat-session-sheet"
        aria-label="Session details"
        onClose={() => onOpenChange(false)}
      >
        <div className="chat-sheet-head">
          <div className="chat-sheet-heading">
            <div className="chat-sheet-bot">{bot}</div>
            {titleEditor}
          </div>
          <button
            type="button"
            className="chat-sheet-close"
            aria-label="Close session details"
            onClick={() => onOpenChange(false)}
          >
            Close
          </button>
        </div>
        <div className="chat-sheet-body">
          {options && <div className="chat-sheet-options">{options}</div>}
          <div className="chat-sheet-meta">{meta}</div>
          {resolved}
          <div className="chat-sheet-actions">{actions}</div>
          <div className="chat-sheet-panels">{children}</div>
        </div>
      </dialog>
    </>
  )
}

/**
 * Desktop thread options: a button in the header that drops a small panel down. Closes on
 * Escape or a click outside, and moves focus into the panel when it opens so a link elsewhere
 * (the composer's model) can open it ready to use.
 */
export function ThreadOptions({
  open,
  onOpenChange,
  children,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  children: ReactNode
}) {
  const root = useRef<HTMLDivElement>(null)
  const panelId = useId()

  useEffect(() => {
    if (!open) return
    const el = root.current
    el?.querySelector<HTMLElement>('[data-options-panel] :is(select, button, a[href], input)')?.focus()
    const onPointer = (event: PointerEvent) => {
      if (event.target instanceof Node && !el?.contains(event.target)) onOpenChange(false)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      onOpenChange(false)
      el?.querySelector<HTMLElement>('[data-options-button]')?.focus()
    }
    document.addEventListener('pointerdown', onPointer)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointer)
      document.removeEventListener('keydown', onKey)
    }
  }, [open, onOpenChange])

  return (
    <div ref={root} className="chat-options relative">
      <button
        type="button"
        data-options-button
        className={`rounded-md border px-2 py-0.5 text-xs ${open ? 'border-indigo-400 text-indigo-700 dark:text-indigo-300' : 'border-zinc-300 text-zinc-600 dark:border-zinc-700 dark:text-zinc-400'}`}
        aria-expanded={open}
        aria-controls={panelId}
        aria-haspopup="true"
        onClick={() => onOpenChange(!open)}
      >
        Options ▾
      </button>
      {open && (
        <div
          id={panelId}
          data-options-panel
          role="group"
          aria-label="Thread options"
          className="absolute right-0 top-full z-30 mt-2 w-80 max-w-[90vw] rounded-card border border-stroke bg-surface p-3 shadow-lg"
        >
          {children}
        </div>
      )}
    </div>
  )
}

/** The composer's one-line model description. It opens the thread options, where the picker is. */
export function ModelLink({ label, onOpen }: { label: string; onOpen: () => void }) {
  return (
    <button
      type="button"
      className="chat-model-link min-w-0 truncate rounded-control text-left text-[11px] text-muted underline decoration-dotted underline-offset-2 hover:text-ink"
      title="Change the model in the thread options"
      aria-haspopup="true"
      onClick={onOpen}
    >
      Model: {label}
    </button>
  )
}
