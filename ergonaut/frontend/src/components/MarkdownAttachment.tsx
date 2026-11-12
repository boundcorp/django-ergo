import { useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { api } from '../api'
import Markdown from './Markdown'
import './MarkdownAttachment.css'

const MAX_SOURCE_BYTES = 500_000

async function readSource(response: Response): Promise<string> {
  if (!response.ok) throw new Error('Could not load this document.')
  const length = Number(response.headers.get('content-length'))
  if (length > MAX_SOURCE_BYTES) throw new Error('Document is too large to read here. Download the original instead.')
  if (!response.body) throw new Error('Could not read this document. Download the original instead.')
  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8', { ignoreBOM: true })
  let size = 0
  let source = ''
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      size += value.byteLength
      if (size > MAX_SOURCE_BYTES) {
        await reader.cancel()
        throw new Error('Document is too large to read here. Download the original instead.')
      }
      source += decoder.decode(value, { stream: true })
    }
    return source + decoder.decode()
  } finally {
    reader.releaseLock()
  }
}

export function MarkdownAttachment({ id, label }: { id: string; label: string }) {
  const [open, setOpen] = useState(false)
  const opener = useRef<HTMLButtonElement>(null)
  return (
    <div className="md-attachment-card">
      <button ref={opener} type="button" onClick={() => setOpen(true)} aria-label={`Read ${label}`}>
        <span aria-hidden="true">▤</span> <span>{label}</span>
        <small>Markdown document · Read</small>
      </button>
      <a href={api.downloadUrl(id)} aria-label={`Download ${label}`}>
        Download original
      </a>
      {open && (
        <Reader
          key={id}
          id={id}
          label={label}
          onClose={() => {
            setOpen(false)
            opener.current?.focus({ preventScroll: true })
          }}
        />
      )}
    </div>
  )
}

function Reader({ id, label, onClose }: { id: string; label: string; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  const [source, setSource] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    let active = true
    dialog.current?.showModal()
    fetch(api.downloadUrl(id), { signal: controller.signal, credentials: 'same-origin' })
      .then(readSource)
      .then(text => {
        if (active) setSource(text)
      })
      .catch((err: unknown) => {
        if (active) setError(err instanceof Error ? err.message : 'Could not load this document.')
      })
    return () => {
      active = false
      controller.abort()
    }
  }, [id])
  const close = () => {
    dialog.current?.close()
    onClose()
  }
  return createPortal(
    <dialog
      ref={dialog}
      className="md-reader"
      aria-labelledby={titleId}
      onCancel={event => {
        event.preventDefault()
        close()
      }}
    >
      <header className="md-reader-header">
        <h2 id={titleId}>{label}</h2>
        <button type="button" autoFocus onClick={close} aria-label="Close reader">
          Close
        </button>
      </header>
      <div className="md-reader-actions">
        <button
          type="button"
          disabled={source === null}
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(source!)
              setStatus('Raw source copied.')
            } catch {
              setStatus('Could not copy raw source.')
            }
          }}
        >
          Copy raw source
        </button>
        <a href={api.downloadUrl(id)}>Download original</a>
        <span role="status" aria-live="polite">
          {status}
        </span>
      </div>
      <div className="md-reader-content">
        {error ? (
          <p role="alert">{error}</p>
        ) : source === null ? (
          <p role="status">Loading document…</p>
        ) : (
          <Markdown text={source} mode="document" />
        )}
      </div>
    </dialog>,
    document.body,
  )
}
