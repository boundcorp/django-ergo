import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { api, type SessionSearchResult } from '../api'
import { ago } from '../time'

// Render text as text, never as HTML, including snippets returned by the server.
export function Highlight({ text, query }: { text: string; query: string }) {
  const needle = query.trim().toLocaleLowerCase()
  if (!needle) return <>{text}</>
  const lower = text.toLocaleLowerCase()
  const parts = []
  let start = 0
  let index = lower.indexOf(needle)
  while (index !== -1) {
    parts.push(text.slice(start, index), <mark key={index}>{text.slice(index, index + needle.length)}</mark>)
    start = index + needle.length
    index = lower.indexOf(needle, start)
  }
  parts.push(text.slice(start))
  return <>{parts}</>
}

export function SessionSearch() {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<SessionSearchResult[]>([])
  const [state, setState] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle')
  const [selected, setSelected] = useState(0)
  const input = useRef<HTMLInputElement>(null)
  const previousFocus = useRef<HTMLElement | null>(null)
  const navigate = useNavigate()

  function show() {
    previousFocus.current = document.activeElement as HTMLElement | null
    setOpen(true)
  }
  function close() {
    setOpen(false)
    setQuery('')
    previousFocus.current?.focus()
  }
  useEffect(() => {
    function shortcut(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        if (!open) show()
        else input.current?.focus()
      }
    }
    window.addEventListener('keydown', shortcut)
    return () => window.removeEventListener('keydown', shortcut)
  }, [open])
  useEffect(() => {
    if (open) input.current?.focus()
  }, [open])
  useEffect(() => {
    const term = query.trim()
    setResults([])
    setSelected(0)
    if (!open || !term) {
      setState('idle')
      return
    }
    setState('loading')
    let current = true
    const timer = window.setTimeout(() => {
      api.searchSessions(term).then(
        rows => {
          if (current) {
            setResults(rows)
            setState('ready')
          }
        },
        () => {
          if (current) setState('error')
        },
      )
    }, 250)
    return () => {
      current = false
      window.clearTimeout(timer)
    }
  }, [open, query])

  function choose(result: SessionSearchResult) {
    close()
    navigate(`/s/${result.id}${result.line != null ? `#m-${result.line}` : ''}`)
  }

  return (
    <>
      <button
        type="button"
        aria-label="Search conversations"
        title="Search conversations (Ctrl/⌘ K)"
        onClick={show}
        className="rounded-lg p-2 text-muted hover:bg-raised hover:text-ink"
      >
        <svg
          aria-hidden="true"
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
        >
          <circle cx="11" cy="11" r="7" />
          <path d="m16 16 4 4" />
        </svg>
      </button>
      {open &&
        createPortal(
          <div
            className="fixed inset-0 z-[100] flex items-center justify-center bg-black/50 p-3"
            onMouseDown={event => {
              if (event.target === event.currentTarget) close()
            }}
          >
            <div
              role="dialog"
              aria-modal="true"
              aria-labelledby="session-search-title"
              className="flex max-h-full w-full max-w-xl flex-col overflow-hidden rounded-xl border border-stroke bg-canvas shadow-xl max-sm:h-full"
              onKeyDown={event => {
                // The mobile navigation has its own document-level Escape and Tab handlers.
                event.stopPropagation()
                if (event.key === 'Escape') {
                  event.preventDefault()
                  close()
                } else if (event.key === 'Tab') {
                  const items = event.currentTarget.querySelectorAll<HTMLElement>('input, button:not([disabled])')
                  const first = items[0],
                    last = items[items.length - 1]
                  if (event.shiftKey && document.activeElement === first) {
                    event.preventDefault()
                    last?.focus()
                  } else if (!event.shiftKey && document.activeElement === last) {
                    event.preventDefault()
                    first?.focus()
                  }
                } else if (event.key === 'ArrowDown' && results.length) {
                  event.preventDefault()
                  setSelected(i => (i + 1) % results.length)
                } else if (event.key === 'ArrowUp' && results.length) {
                  event.preventDefault()
                  setSelected(i => (i + results.length - 1) % results.length)
                } else if (event.key === 'Enter' && document.activeElement === input.current && results[selected]) {
                  event.preventDefault()
                  choose(results[selected])
                }
              }}
            >
              <div className="flex items-center gap-2 border-b border-stroke p-4">
                <label id="session-search-title" htmlFor="session-search-input" className="sr-only">
                  Search conversations
                </label>
                <input
                  ref={input}
                  id="session-search-input"
                  type="search"
                  value={query}
                  onChange={event => setQuery(event.target.value)}
                  placeholder="Search conversations…"
                  autoComplete="off"
                  className="min-w-0 flex-1 bg-transparent text-ink outline-none"
                  aria-controls="session-search-results"
                  aria-activedescendant={results[selected] ? `search-result-${selected}` : undefined}
                  role="combobox"
                  aria-expanded="true"
                />
                <button
                  type="button"
                  onClick={close}
                  className="rounded px-2 py-1 text-sm text-muted hover:bg-raised"
                  aria-label="Close search"
                >
                  Esc
                </button>
              </div>
              <div
                id="session-search-results"
                role="listbox"
                aria-label="Search results"
                className="min-h-0 max-h-[60vh] overflow-y-auto p-2 max-sm:max-h-none max-sm:flex-1"
              >
                {results.map((result, index) => (
                  <button
                    type="button"
                    role="option"
                    aria-selected={index === selected}
                    id={`search-result-${index}`}
                    key={result.id}
                    onMouseEnter={() => setSelected(index)}
                    onClick={() => choose(result)}
                    className={`block w-full rounded-lg px-3 py-2 text-left ${index === selected ? 'bg-indigo-tint' : 'hover:bg-raised'}`}
                  >
                    <span className="block font-semibold">
                      <Highlight text={result.title} query={query} />
                    </span>
                    <span className="block text-xs text-muted">
                      {result.bot} · {result.resolved ? 'Resolved' : 'Active'}
                      {result.last_activity ? ` · ${ago(result.last_activity)}` : ''}
                    </span>
                    {result.snippet && (
                      <span className="mt-1 block text-sm text-muted">
                        <Highlight text={result.snippet} query={query} />
                      </span>
                    )}
                  </button>
                ))}
              </div>
              <div role="status" aria-live="polite" className="px-5 pb-3 text-sm text-muted">
                {state === 'idle'
                  ? 'Type to search conversation titles and messages.'
                  : state === 'loading'
                    ? 'Searching…'
                    : state === 'error'
                      ? 'Search is unavailable. Try again.'
                      : !results.length
                        ? 'No conversations found.'
                        : `${results.length} result${results.length === 1 ? '' : 's'}`}
              </div>
            </div>
          </div>,
          document.body,
        )}
    </>
  )
}
