import { useEffect, useRef, useState } from 'react'

/** Copies text on click. The clipboard is only touched inside the handler, so this is SSR-safe. */
export function CopyButton({ text, label, className = '' }: { text: string; label: string; className?: string }) {
  const [copied, setCopied] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  useEffect(() => () => clearTimeout(timer.current), [])
  return (
    <>
      <button
        type="button"
        className={`ergo-chat__btn ergo-chat__btn--small ${className}`.trim()}
        aria-label={`Copy ${label}`}
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(text)
            setCopied(true)
            clearTimeout(timer.current)
            timer.current = setTimeout(() => setCopied(false), 1500)
          } catch {
            // The clipboard needs a secure context and permission; there is nothing useful to show.
          }
        }}
      >
        {copied ? 'Copied' : 'Copy'}
      </button>
      <span className="ergo-chat__sr" role="status">
        {copied ? `Copied ${label}` : ''}
      </span>
    </>
  )
}
