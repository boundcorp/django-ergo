import { useEffect, useRef, useState } from 'react'
import type { IllustrationJob, IllustrationResult, IllustrationSuggestion } from '../api'
import { api } from '../api'

export const MAX_SELECTIONS = 3

export function toggleSuggestion(selected: string[], id: string): string[] {
  if (selected.includes(id)) return selected.filter(item => item !== id)
  return selected.length < MAX_SELECTIONS ? [...selected, id] : selected
}

export function IllustrateDialog({
  sessionId,
  disabled,
  onCreated,
}: {
  sessionId: string
  disabled: boolean
  onCreated: (result: IllustrationResult) => void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const opener = useRef<HTMLButtonElement>(null)
  const requestVersion = useRef(0)
  const creating = useRef(false)
  const [loading, setLoading] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [error, setError] = useState('')
  const [snapshot, setSnapshot] = useState('')
  const [asOf, setAsOf] = useState(0)
  const [suggestions, setSuggestions] = useState<IllustrationSuggestion[]>([])
  const [selected, setSelected] = useState<string[]>([])
  const [custom, setCustom] = useState('')
  const [job, setJob] = useState<IllustrationJob | null>(null)

  useEffect(
    () => () => {
      requestVersion.current += 1
      dialog.current?.close()
    },
    [],
  )

  useEffect(() => {
    if (!job || !['queued', 'running', 'retrying'].includes(job.status)) return
    let cancelled = false
    const timer = window.setTimeout(async () => {
      try {
        const next = await api.illustration(sessionId, job.id)
        if (cancelled) return
        setJob(next)
        if (next.status === 'succeeded' && next.attachment) {
          onCreated({ attachment: next.attachment, summary: next.summary, as_of_line: next.as_of_line })
          close()
        } else if (next.status === 'failed' || next.status === 'cancelled') {
          setGenerating(false)
          setError(
            next.error ||
              (next.status === 'cancelled'
                ? 'Illustration generation was cancelled.'
                : 'Illustration generation failed.'),
          )
        }
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : String(e))
          setJob(current => (current?.id === job.id ? { ...current } : current))
        }
      }
    }, 750)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [job, onCreated, sessionId])
  async function open() {
    const version = ++requestVersion.current
    setError('')
    setSnapshot('')
    setAsOf(0)
    setSelected([])
    setCustom('')
    setJob(null)
    setSuggestions([])
    dialog.current?.showModal()
    setLoading(true)
    try {
      const response = await api.illustrateSuggestions(sessionId)
      if (requestVersion.current !== version) return
      setSnapshot(response.snapshot)
      setAsOf(response.as_of_line)
      setSuggestions(response.suggestions)
    } catch (e) {
      if (requestVersion.current === version) setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (requestVersion.current === version) setLoading(false)
    }
  }
  function close() {
    requestVersion.current += 1
    dialog.current?.close()
    opener.current?.focus()
  }
  function toggle(id: string) {
    setSelected(items => toggleSuggestion(items, id))
  }
  async function create() {
    if (creating.current || !snapshot || (!selected.length && !custom.trim())) return
    creating.current = true
    setGenerating(true)
    setError('')
    try {
      const next = await api.illustrate(sessionId, snapshot, selected, custom)
      setJob(next)
      if (next.status === 'succeeded' && next.attachment) {
        setGenerating(false)
        onCreated({ attachment: next.attachment, summary: next.summary, as_of_line: next.as_of_line })
        close()
      } else if (next.status === 'failed' || next.status === 'cancelled') {
        setGenerating(false)
        setError(
          next.error ||
            (next.status === 'cancelled'
              ? 'Illustration generation was cancelled.'
              : 'Illustration generation failed.'),
        )
      }
    } catch (e) {
      setGenerating(false)
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      creating.current = false
    }
  }
  async function cancel() {
    if (!job || !generating) return
    try {
      setJob(await api.cancelIllustration(sessionId, job.id))
      setGenerating(false)
      setError('Illustration generation was cancelled.')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }
  return (
    <>
      <button
        ref={opener}
        type="button"
        disabled={disabled}
        onClick={open}
        title="Create a static illustration from this chat"
        className="illustrate-action self-end rounded-full border border-indigo-400 bg-surface px-3 py-1.5 text-xs font-semibold text-indigo-700 shadow-sm hover:bg-indigo-tint disabled:opacity-50 dark:text-indigo-300"
      >
        ✦ Illustrate
      </button>
      <dialog
        ref={dialog}
        aria-labelledby="illustrate-title"
        onCancel={event => {
          if (generating) event.preventDefault()
        }}
        className="m-auto w-[min(92vw,42rem)] rounded-card border border-stroke bg-surface p-0 text-ink shadow-xl backdrop:bg-black/40"
      >
        <form method="dialog" className="border-b border-stroke p-4">
          <div className="flex items-start gap-4">
            <div>
              <h2 id="illustrate-title" className="font-display text-xl font-bold">
                Illustrate this chat
              </h2>
              <p className="mt-1 text-sm text-muted">
                Suggestions use messages through #{asOf || '…'}. Select up to three, or describe something else.
              </p>
            </div>
            <button
              type="submit"
              disabled={generating}
              onClick={close}
              className="ml-auto min-h-11 min-w-11 rounded-control text-muted hover:bg-raised disabled:opacity-50"
              aria-label="Close illustration picker"
            >
              ×
            </button>
          </div>
        </form>
        <div className="max-h-[60dvh] overflow-y-auto p-4">
          {loading && (
            <p className="text-sm text-muted" aria-live="polite">
              Finding evidence-grounded visual options…
            </p>
          )}
          {!loading &&
            suggestions.map(suggestion => (
              <label
                key={suggestion.id}
                className="mb-2 flex cursor-pointer gap-3 rounded-card border border-stroke p-3 hover:bg-raised"
              >
                <input
                  type="checkbox"
                  checked={selected.includes(suggestion.id)}
                  disabled={
                    generating ||
                    suggestion.availability !== 'available' ||
                    (!selected.includes(suggestion.id) && selected.length >= MAX_SELECTIONS)
                  }
                  onChange={() => toggle(suggestion.id)}
                />
                <span>
                  <strong>{suggestion.title}</strong>
                  <span className="block text-sm text-muted">{suggestion.purpose}</span>
                  <span className="block text-xs text-muted">
                    {suggestion.reason}
                    {suggestion.evidence_refs.length
                      ? ` · messages ${suggestion.evidence_refs.map(line => `#${line}`).join(', ')}`
                      : ''}
                  </span>
                </span>
              </label>
            ))}
          {!loading && (
            <label className="mt-4 block text-sm font-medium" htmlFor="illustrate-custom">
              Something else <span className="font-normal text-muted">(optional)</span>
              <textarea
                id="illustrate-custom"
                value={custom}
                disabled={generating}
                maxLength={500}
                onChange={event => setCustom(event.target.value)}
                rows={3}
                placeholder="Describe the static visual you need from this chat"
                className="mt-1 block w-full rounded-control border border-stroke bg-canvas p-2 font-normal"
              />
              <span className="text-xs font-normal text-muted">
                {custom.length}/500 · Uses this chat snapshot only.
              </span>
            </label>
          )}
          {generating && job && (
            <p role="status" className="mt-3 text-sm text-muted">
              {job.status === 'retrying' ? 'Retrying illustration generation' : 'Creating illustration'} · attempt{' '}
              {Math.max(1, job.attempts)} of 3
            </p>
          )}
          {error && (
            <p role="alert" className="mt-3 rounded-control bg-red-tint p-2 text-sm text-danger">
              {error}
            </p>
          )}
        </div>
        <div className="flex justify-end gap-2 border-t border-stroke p-4">
          <button
            type="button"
            disabled={!generating}
            onClick={cancel}
            className="rounded-control px-3 py-2 text-sm disabled:opacity-50"
          >
            Cancel generation
          </button>
          <button
            type="button"
            disabled={generating}
            onClick={close}
            className="rounded-control px-3 py-2 text-sm disabled:opacity-50"
          >
            Close
          </button>
          <button
            type="button"
            disabled={loading || generating || !snapshot || (!selected.length && !custom.trim())}
            onClick={create}
            className="rounded-control bg-accent px-4 py-2 text-sm font-semibold text-canvas disabled:opacity-50"
          >
            {generating ? 'Creating…' : 'Create illustration'}
          </button>
        </div>
      </dialog>
    </>
  )
}
