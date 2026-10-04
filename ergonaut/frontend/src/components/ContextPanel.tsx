import { useState } from 'react'
import type { Call, Compaction, TurnContext } from '../api'
import { api } from '../api'
import Markdown from './Markdown'

export function ContextPanel({ call }: { call: Call }) {
  const [open, setOpen] = useState(false)
  const [context, setContext] = useState<TurnContext | null>(null)
  const [error, setError] = useState('')
  if (!call.context) return null
  async function toggle() {
    setOpen(!open)
    if (!open && !context) {
      try {
        setContext(await api.callContext(call.id))
      } catch (e) {
        setError(String(e))
      }
    }
  }
  return (
    <div className="mt-2 text-xs text-muted">
      <button className="hover:text-ink" onClick={toggle} aria-expanded={open}>
        Context
      </button>
      {open && (
        <div className="mt-2 rounded-card border border-stroke bg-surface p-3 text-ink">
          {error ||
            (!context ? (
              'Loading…'
            ) : (
              <>
                <p>
                  {context.prompt_tokens.toLocaleString()} prompt tokens (turn total) /{' '}
                  {context.context_window.toLocaleString()} window
                </p>
                <p className="mt-1 text-muted">
                  Estimated: {context.estimated_tokens.toLocaleString()} · Compact at:{' '}
                  {context.compact_at_tokens?.toLocaleString() ?? 'off'}
                </p>
                <p className="mt-1">
                  {context.native_messages.count} native messages (oldest #
                  {context.native_messages.first_sequence ?? '—'}) · {context.stubbed_results} tool results stubbed
                </p>
                {context.sections.map((section, i) => (
                  <details key={i} className="mt-2">
                    <summary className="cursor-pointer">
                      {section.title} · {section.tokens.toLocaleString()} tokens{section.complete ? '' : ' · truncated'}
                    </summary>
                    <pre className="mt-1 whitespace-pre-wrap break-words">{section.text}</pre>
                  </details>
                ))}
                {context.compaction && (
                  <details className="mt-2">
                    <summary className="cursor-pointer">
                      Compaction through #{context.compaction.upto_sequence} · {context.compaction.message_count}{' '}
                      messages
                    </summary>
                    <p className="my-1 text-muted">
                      {context.compaction.reason} · {context.compaction.created_at}
                    </p>
                    <Markdown text={context.compaction.summary ?? ''} />
                  </details>
                )}
              </>
            ))}
        </div>
      )}
    </div>
  )
}

export function CompactionDivider({ compaction, sessionId }: { compaction: Compaction; sessionId: string }) {
  const [summary, setSummary] = useState<string | null>(null)
  const [error, setError] = useState('')
  return (
    <details
      className="my-4 border-t border-stroke pt-3 text-xs text-muted"
      onToggle={async event => {
        if (event.currentTarget.open && summary === null) {
          try {
            setSummary((await api.compaction(sessionId, compaction.id)).summary)
          } catch (e) {
            setError(String(e))
          }
        }
      }}
    >
      <summary className="cursor-pointer text-center">
        Earlier messages summarized ({compaction.message_count} messages)
      </summary>
      <div className="mt-2 text-ink">{error || (summary === null ? 'Loading…' : <Markdown text={summary} />)}</div>
    </details>
  )
}
