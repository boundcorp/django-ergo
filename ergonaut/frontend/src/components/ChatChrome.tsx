import { useEffect, useId, useRef, type ReactNode } from 'react'

/** Suggested replies as real buttons. Phone CSS raises them to a 44px target. */
export function SuggestionChips({
  suggestions,
  disabled,
  onPick,
}: {
  suggestions: string[]
  disabled?: boolean
  onPick: (text: string) => void
}) {
  if (!suggestions.length) return null
  return (
    <div className="suggestion-mobile suggestion-row" role="group" aria-label="Suggested replies">
      {suggestions.map(suggestion => (
        <button
          key={suggestion}
          type="button"
          className="suggestion-chip"
          disabled={disabled}
          onClick={() => onPick(suggestion)}
        >
          {suggestion}
        </button>
      ))}
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
            <h2 className="chat-sheet-title">{title}</h2>
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
