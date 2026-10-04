import { useCallback, useEffect, useRef, useState } from 'react'
import type { AttachmentFile, Pin } from '../api'
import { api } from '../api'
import { PrChip } from './Threads'

const SOURCE_LABEL: Record<AttachmentFile['source'], string> = {
  message: 'sent',
  upload: 'uploaded',
  bot: 'by the bot',
}

function size(bytes: number | null) {
  if (bytes == null) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

// The session's files: uploads, files sent with messages, and files the bot wrote.
// Pages, images, PDFs, Markdown, text and media open in the chat's viewer; any file can be pinned.

export default function Files({
  sessionId,
  refreshKey,
  onPinsChanged,
  onView,
}: {
  sessionId: string
  refreshKey: unknown
  onPinsChanged: () => void
  onView: (pin: Pin) => void
}) {
  const [files, setFiles] = useState<AttachmentFile[] | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [showArchived, setShowArchived] = useState(false)
  const input = useRef<HTMLInputElement>(null)
  const archivedCount = files?.filter(f => f.archived_at).length ?? 0
  const shown = files?.filter(f => showArchived || !f.archived_at)

  const load = useCallback(
    () =>
      api
        .attachments(sessionId)
        .then(setFiles)
        .catch(e => setError(String(e.message ?? e))),
    [sessionId],
  )
  useEffect(() => {
    load()
  }, [load, refreshKey])

  async function uploadFiles(list: FileList | null) {
    if (!list?.length) return
    setBusy(true)
    setError('')
    try {
      for (const file of Array.from(list)) await api.uploadAttachment(sessionId, file)
      await load()
    } catch (e) {
      setError(String((e as Error).message ?? e))
    } finally {
      setBusy(false)
      if (input.current) input.current.value = ''
    }
  }

  async function togglePin(file: AttachmentFile) {
    await api.pin(file.id, !file.pinned).catch(e => setError(String(e.message ?? e)))
    await load()
    onPinsChanged()
  }

  async function remove(file: AttachmentFile) {
    if (!confirm(`Delete ${file.filename}?`)) return
    await api.deleteAttachment(file.id).catch(e => setError(String(e.message ?? e)))
    await load()
  }

  return (
    <aside className="flex w-72 shrink-0 flex-col border-l border-zinc-200 dark:border-zinc-800">
      <div className="flex items-center border-b border-zinc-200 px-4 py-3 dark:border-zinc-800">
        <div className="font-semibold">Files</div>
        <button
          disabled={busy}
          className="ml-auto rounded-md border border-zinc-300 px-2 py-0.5 text-sm hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-900"
          onClick={() => input.current?.click()}
        >
          {busy ? 'Uploading…' : 'Upload'}
        </button>
        <input ref={input} type="file" multiple hidden onChange={e => uploadFiles(e.target.files)} />
      </div>
      <div
        className="flex-1 overflow-y-auto p-2"
        onDragOver={e => e.preventDefault()}
        onDrop={e => {
          e.preventDefault()
          uploadFiles(e.dataTransfer.files)
        }}
      >
        {error && <div className="p-2 text-sm text-red-600">{error}</div>}
        {files && !files.length && (
          <p className="p-2 text-sm text-zinc-500">No files yet. Upload or drop files here; the bot can read them.</p>
        )}
        {shown?.map(file => (
          <div
            key={file.id}
            className={`group rounded-md px-2 py-1.5 hover:bg-zinc-100 dark:hover:bg-zinc-900 ${file.archived_at ? 'opacity-60' : ''}`}
          >
            {file.link ? (
              <div className="flex items-center gap-2">
                <PrChip pr={file.link} />
              </div>
            ) : (
              <div className="flex items-center gap-2">
                {file.view ? (
                  <button
                    className="truncate text-left text-sm text-indigo-600 hover:underline dark:text-indigo-400"
                    title={`View ${file.filename}`}
                    onClick={() =>
                      onView({ kind: 'file', name: file.filename, id: file.id, url: api.viewUrl(file.id) })
                    }
                  >
                    {file.filename || file.media_type}
                  </button>
                ) : (
                  <a
                    className="truncate text-sm text-indigo-600 hover:underline dark:text-indigo-400"
                    href={api.downloadUrl(file.id)}
                    title={file.filename}
                  >
                    {file.filename || file.media_type}
                  </a>
                )}
                <a
                  className="text-xs text-zinc-400 hover:text-zinc-700 dark:hover:text-zinc-100"
                  href={api.downloadUrl(file.id)}
                  title="Download"
                >
                  ⬇
                </a>
                <button
                  className={`ml-auto text-xs ${file.pinned ? '' : 'hidden opacity-50 group-hover:block'}`}
                  title={file.pinned ? 'Unpin' : 'Pin to the top of the chat'}
                  onClick={() => togglePin(file)}
                >
                  📌
                </button>
                {file.message_sequence == null && (
                  <button
                    className="hidden text-xs text-zinc-400 hover:text-red-600 group-hover:block"
                    title="Delete"
                    onClick={() => remove(file)}
                  >
                    ✕
                  </button>
                )}
              </div>
            )}
            <div className="text-xs text-zinc-500">
              {SOURCE_LABEL[file.source]} · {size(file.size)} · {new Date(file.updated_at).toLocaleString()}
              {file.archived_at && ' · archived'}
            </div>
          </div>
        ))}
        {archivedCount > 0 && (
          <button
            className="p-2 text-xs text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-300"
            onClick={() => setShowArchived(v => !v)}
          >
            {showArchived ? 'Hide archived' : `Show ${archivedCount} archived`}
          </button>
        )}
      </div>
    </aside>
  )
}
