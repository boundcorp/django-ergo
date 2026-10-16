import { useEffect, useRef, useState } from 'react'
import type { IllustrationResult, IllustrationSuggestion } from '../api'
import { api } from '../api'

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
  const [loading, setLoading] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [error, setError] = useState('')
  const [snapshot, setSnapshot] = useState('')
  const [asOf, setAsOf] = useState(0)
  const [suggestions, setSuggestions] = useState<IllustrationSuggestion[]>([])
  const [selected, setSelected] = useState<string[]>([])
  const [custom, setCustom] = useState('')

  useEffect(() => () => dialog.current?.close(), [])
  async function open() {
    setError('')
    setSelected([])
    setCustom('')
    setSuggestions([])
    dialog.current?.showModal()
    setLoading(true)
    try {
      const response = await api.illustrateSuggestions(sessionId)
      setSnapshot(response.snapshot)
      setAsOf(response.as_of_line)
      setSuggestions(response.suggestions)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }
  function close() {
    dialog.current?.close()
    opener.current?.focus()
  }
  function toggle(id: string) {
    setSelected(items =>
      items.includes(id) ? items.filter(item => item !== id) : items.length < 3 ? [...items, id] : items,
    )
  }
  async function create() {
    if (!snapshot || (!selected.length && !custom.trim())) return
    setGenerating(true)
    setError('')
    try {
      const result = await api.illustrate(sessionId, snapshot, selected, custom)
      onCreated(result)
      close()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setGenerating(false)
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
              onClick={close}
              className="ml-auto min-h-11 min-w-11 rounded-control text-muted hover:bg-raised"
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
                    suggestion.availability !== 'available' ||
                    (!selected.includes(suggestion.id) && selected.length >= 3)
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
          {error && (
            <p role="alert" className="mt-3 rounded-control bg-red-tint p-2 text-sm text-danger">
              {error}
            </p>
          )}
        </div>
        <div className="flex justify-end gap-2 border-t border-stroke p-4">
          <button type="button" onClick={close} className="rounded-control px-3 py-2 text-sm">
            Cancel
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
