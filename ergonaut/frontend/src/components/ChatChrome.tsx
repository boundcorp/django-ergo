import { useRef, useState, type ReactNode } from 'react'

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
 * Phone-only session bar. The title and counts stay one line; the sheet holds the
 * meta, actions, waiting list, worker cards and pins. Those children stay mounted,
 * so a live session update refreshes the counts and the open sheet together.
 */
export function MobileSessionBar({
  title,
  summary,
  meta,
  resolved,
  actions,
  children,
}: {
  title: string
  summary: string
  meta: ReactNode
  resolved?: ReactNode
  actions: ReactNode
  children: ReactNode
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [open, setOpen] = useState(false)
  const label = summary || 'Session details'

  function show() {
    dialog.current?.showModal()
    setOpen(true)
  }

  return (
    <>
      <div className="chat-mobile-bar">
        <button
          type="button"
          className="chat-mobile-summary"
          aria-expanded={open}
          aria-controls="chat-session-sheet"
          aria-haspopup="dialog"
          aria-label={`${title}. ${label}. Open session details`}
          data-status-summary={summary}
          onClick={show}
        >
          <span className="chat-mobile-title">{title}</span>
          <span className="chat-mobile-counts">{summary}</span>
        </button>
      </div>
      <dialog
        ref={dialog}
        id="chat-session-sheet"
        className="chat-session-sheet"
        aria-label="Session details"
        onClose={() => setOpen(false)}
      >
        <div className="chat-sheet-head">
          <h2 className="chat-sheet-title">{title}</h2>
          <button
            type="button"
            className="chat-sheet-close"
            aria-label="Close session details"
            onClick={() => dialog.current?.close()}
          >
            Close
          </button>
        </div>
        <div className="chat-sheet-body">
          <div className="chat-sheet-meta">{meta}</div>
          {resolved}
          <div className="chat-sheet-actions">{actions}</div>
          <div className="chat-sheet-panels">{children}</div>
        </div>
      </dialog>
    </>
  )
}
