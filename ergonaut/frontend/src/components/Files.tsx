import { useCallback, useEffect, useRef, useState } from 'react'
import type { AttachmentFile } from '../api'
import { api } from '../api'

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
export default function Files({ sessionId, refreshKey }: { sessionId: string; refreshKey: unknown }) {
  const [files, setFiles] = useState<AttachmentFile[] | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const input = useRef<HTMLInputElement>(null)

  const load = useCallback(
    () => api.attachments(sessionId).then(setFiles).catch(e => setError(String(e.message ?? e))),
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
        {files?.map(file => (
          <div key={file.id} className="group rounded-md px-2 py-1.5 hover:bg-zinc-100 dark:hover:bg-zinc-900">
            <div className="flex items-center gap-2">
              <a className="truncate text-sm text-indigo-600 hover:underline dark:text-indigo-400" href={api.downloadUrl(file.id)} title={file.filename}>
                {file.filename || file.media_type}
              </a>
              {file.message_sequence == null && (
                <button
                  className="ml-auto hidden text-xs text-zinc-400 hover:text-red-600 group-hover:block"
                  title="Delete"
                  onClick={() => remove(file)}
                >
                  ✕
                </button>
              )}
            </div>
            <div className="text-xs text-zinc-500">
              {SOURCE_LABEL[file.source]} · {size(file.size)} · {new Date(file.updated_at).toLocaleString()}
            </div>
          </div>
        ))}
      </div>
    </aside>
  )
}
