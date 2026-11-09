import { useCallback, useEffect, useRef, useState } from 'react'
import type { AttachmentFile, Pin } from '../api'
import { api } from '../api'
import { PrChip } from './Threads'

const SOURCE_LABEL: Record<AttachmentFile['source'], string> = {
  message: 'sent',
  upload: 'uploaded',
  bot: 'by the bot',
}

export type FileCategory = 'image' | 'media' | 'doc' | 'link'
export type FileFilter = 'all' | 'image' | 'media' | 'doc'

const FILTERS: { key: FileFilter; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'image', label: 'Images' },
  { key: 'doc', label: 'Docs' },
  { key: 'media', label: 'Media' },
]

const ICON: Record<string, string> = {
  page: '🗂️',
  html: '🌐',
  pdf: '📕',
  markdown: '📝',
  csv: '📊',
  json: '🧾',
  text: '📄',
  media: '🎵',
  image: '🖼️',
}

/** Which tab of the panel a file belongs to. */
export function fileCategory(file: Pick<AttachmentFile, 'view' | 'kind' | 'media_type' | 'link'>): FileCategory {
  if (file.link) return 'link'
  if (file.view === 'image' || file.kind === 'image') return 'image'
  if (file.view === 'media' || file.kind === 'audio' || file.kind === 'video') return 'media'
  if (/^(audio|video)\//.test(file.media_type)) return 'media'
  return 'doc'
}

/** The files a filter and a search show, in order. */
export function filterFiles(files: AttachmentFile[], filter: FileFilter, query: string, showArchived: boolean) {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean)
  return files.filter(file => {
    if (!showArchived && file.archived_at) return false
    if (filter !== 'all' && fileCategory(file) !== filter) return false
    const haystack = [file.filename, file.media_type, file.shared_from?.label ?? '', file.link?.title ?? '']
      .join(' ')
      .toLowerCase()
    return words.every(word => haystack.includes(word))
  })
}

function size(bytes: number | null) {
  if (bytes == null) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function when(iso: string) {
  const date = new Date(iso)
  const sameYear = date.getFullYear() === new Date().getFullYear()
  return date.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    ...(sameYear ? {} : { year: 'numeric' }),
    hour: 'numeric',
    minute: '2-digit',
  })
}

function name(file: AttachmentFile) {
  return file.filename || file.media_type
}

// The session's files: uploads, files sent with messages, files the bot wrote, and files other
// chats shared with this one (those stay in the chat that sent them). Images show a preview.
// Pages, images, PDFs, Markdown, text and media open in the chat's viewer; this chat's files can be pinned.

export default function Files({
  sessionId,
  refreshKey,
  onPinsChanged,
  onView,
  onClose,
}: {
  sessionId: string
  refreshKey: unknown
  onPinsChanged: () => void
  onView: (pin: Pin) => void
  onClose: () => void
}) {
  const [files, setFiles] = useState<AttachmentFile[] | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [showArchived, setShowArchived] = useState(false)
  const [filter, setFilter] = useState<FileFilter>('all')
  const [query, setQuery] = useState('')
  const input = useRef<HTMLInputElement>(null)
  const archivedCount = files?.filter(f => f.archived_at).length ?? 0
  const shown = files ? filterFiles(files, filter, query, showArchived) : []
  const own = shown.filter(f => !f.shared_from)
  const shared = shown.filter(f => f.shared_from)
  const counts = (key: FileFilter) =>
    (files ?? []).filter(f => (showArchived || !f.archived_at) && (key === 'all' || fileCategory(f) === key)).length

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
  useEffect(() => {
    setFilter('all')
    setQuery('')
  }, [sessionId])

  async function uploadFiles(list: FileList | null) {
    if (!list?.length || busy) return
    setBusy(true)
    setError('')
    try {
      for (const file of Array.from(list)) await api.uploadAttachment(sessionId, file)
      await load()
    } catch (e) {
      await load()
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

  function view(file: AttachmentFile) {
    onView({ kind: 'file', name: file.filename, id: file.id, url: api.viewUrl(file.id), view: file.view })
  }

  const gallery = filter === 'image'
  const section = (list: AttachmentFile[]) =>
    gallery ? (
      <div className="grid grid-cols-3 gap-1.5 px-1">
        {list.map(file => (
          <button
            key={file.id}
            type="button"
            className={`group relative aspect-square overflow-hidden rounded-md border border-zinc-200 bg-zinc-100 dark:border-zinc-800 dark:bg-zinc-900 ${file.archived_at ? 'opacity-60' : ''}`}
            title={`${name(file)} · ${when(file.created_at)}`}
            aria-label={`View ${name(file)}`}
            onClick={() => view(file)}
          >
            <Thumb file={file} className="h-full w-full" />
          </button>
        ))}
      </div>
    ) : (
      list.map(file => (
        <FileRow
          key={file.id}
          file={file}
          onView={() => view(file)}
          onPin={() => togglePin(file)}
          onDelete={() => remove(file)}
        />
      ))
    )

  return (
    <aside
      id="chat-files"
      aria-label="Files"
      className="absolute inset-y-0 right-0 z-30 flex w-[min(18rem,100%)] flex-col border-l border-zinc-200 bg-white shadow-xl dark:border-zinc-800 dark:bg-zinc-950 sm:static sm:w-72 sm:shrink-0 sm:shadow-none"
    >
      <div className="flex items-center border-b border-zinc-200 px-4 py-3 dark:border-zinc-800">
        <div className="font-semibold">Files</div>
        {!!files?.length && <div className="ml-2 text-xs text-zinc-500">{files.length}</div>}
        <button
          type="button"
          disabled={busy}
          className="ml-auto rounded-md border border-zinc-300 px-2 py-0.5 text-sm hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-900"
          onClick={() => input.current?.click()}
        >
          {busy ? 'Uploading…' : 'Upload'}
        </button>
        <button
          type="button"
          aria-label="Close files"
          className="ml-2 rounded-md px-2 py-0.5 text-sm hover:bg-zinc-100 dark:hover:bg-zinc-900"
          onClick={onClose}
        >
          ✕
        </button>
        <input ref={input} type="file" multiple hidden onChange={e => uploadFiles(e.target.files)} />
      </div>
      {!!files?.length && (
        <div className="space-y-2 border-b border-zinc-200 px-3 py-2 dark:border-zinc-800">
          {files.length > 5 && (
            <input
              type="search"
              value={query}
              onChange={e => setQuery(e.target.value)}
              placeholder="Search files"
              aria-label="Search files"
              className="w-full rounded-md border border-zinc-300 bg-transparent px-2 py-1 text-sm dark:border-zinc-700"
            />
          )}
          <div role="tablist" aria-label="File types" className="flex flex-wrap gap-1">
            {FILTERS.filter(f => f.key === 'all' || counts(f.key) > 0).map(f => (
              <button
                key={f.key}
                type="button"
                role="tab"
                aria-selected={filter === f.key}
                className={`rounded-full border px-2 py-0.5 text-xs ${filter === f.key ? 'border-indigo-400 bg-indigo-50 text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300' : 'border-zinc-300 text-zinc-600 hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-400 dark:hover:bg-zinc-900'}`}
                onClick={() => setFilter(f.key)}
              >
                {f.label} <span className="opacity-60">{counts(f.key)}</span>
              </button>
            ))}
          </div>
        </div>
      )}
      <div
        className="flex-1 overflow-y-auto p-2"
        onDragOver={e => e.preventDefault()}
        onDrop={e => {
          e.preventDefault()
          uploadFiles(e.dataTransfer.files)
        }}
      >
        {error && (
          <div role="alert" className="p-2 text-sm text-red-600">
            {error}
          </div>
        )}
        {files && !files.length && (
          <p className="p-2 text-sm text-zinc-500">No files yet. Upload or drop files here; the bot can read them.</p>
        )}
        {files && !!files.length && !shown.length && <p className="p-2 text-sm text-zinc-500">No files match.</p>}
        {section(own)}
        {!!shared.length && (
          <>
            <h3 className="px-2 pb-1 pt-3 text-xs font-medium uppercase tracking-wide text-zinc-500">
              Shared with this chat
            </h3>
            {section(shared)}
          </>
        )}
        {archivedCount > 0 && (
          <button
            type="button"
            aria-expanded={showArchived}
            className="p-2 text-xs text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-100"
            onClick={() => setShowArchived(v => !v)}
          >
            {showArchived ? 'Hide archived' : `Show ${archivedCount} archived`}
          </button>
        )}
      </div>
    </aside>
  )
}

/** An image's preview, or the file type's icon (also when the preview fails to load). */
function Thumb({ file, className }: { file: AttachmentFile; className: string }) {
  const [failed, setFailed] = useState(false)
  if (file.thumbnail && !failed)
    return (
      <img
        src={file.thumbnail}
        alt=""
        loading="lazy"
        decoding="async"
        className={`${className} object-cover`}
        onError={() => setFailed(true)}
      />
    )
  return (
    <span className={`${className} flex items-center justify-center text-lg`} aria-hidden>
      {ICON[file.view] ?? (fileCategory(file) === 'media' ? '🎵' : '📎')}
    </span>
  )
}

function FileRow({
  file,
  onView,
  onPin,
  onDelete,
}: {
  file: AttachmentFile
  onView: () => void
  onPin: () => void
  onDelete: () => void
}) {
  if (file.link)
    return (
      <div className={`rounded-md px-2 py-1.5 ${file.archived_at ? 'opacity-60' : ''}`}>
        <div className="flex items-center gap-2">
          <PrChip pr={file.link} />
        </div>
        <div className="text-xs text-zinc-500">{when(file.updated_at)}</div>
      </div>
    )
  const label = name(file)
  const where = file.shared_from ? `from ${file.shared_from.label || 'another chat'}` : SOURCE_LABEL[file.source]
  return (
    <div
      className={`group flex items-center gap-2 rounded-md px-2 py-1.5 hover:bg-zinc-100 dark:hover:bg-zinc-900 ${file.archived_at ? 'opacity-60' : ''}`}
    >
      <button
        type="button"
        tabIndex={-1}
        aria-hidden
        className="h-10 w-10 shrink-0 overflow-hidden rounded border border-zinc-200 bg-zinc-50 dark:border-zinc-800 dark:bg-zinc-900"
        onClick={() => (file.view ? onView() : window.open(api.downloadUrl(file.id)))}
      >
        <Thumb file={file} className="h-full w-full" />
      </button>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          {file.view ? (
            <button
              className="truncate text-left text-sm text-indigo-600 hover:underline dark:text-indigo-400"
              title={`View ${file.filename}`}
              onClick={onView}
            >
              {label}
            </button>
          ) : (
            <a
              className="truncate text-sm text-indigo-600 hover:underline dark:text-indigo-400"
              href={api.downloadUrl(file.id)}
              title={file.filename}
            >
              {label}
            </a>
          )}
          <a
            className="text-xs text-zinc-400 hover:text-zinc-700 dark:hover:text-zinc-100"
            href={api.downloadUrl(file.id)}
            aria-label={`Download ${label}`}
            title="Download"
          >
            ⬇
          </a>
          {/* A shared file is pinned or deleted in the chat it belongs to. */}
          {!file.shared_from && (
            <button
              className={`ml-auto text-xs ${file.pinned ? '' : 'opacity-50 hover:opacity-100'}`}
              type="button"
              aria-label={`Pin ${label}`}
              aria-pressed={file.pinned}
              title={file.pinned ? 'Unpin' : 'Pin to the top of the chat'}
              onClick={onPin}
            >
              📌
            </button>
          )}
          {!file.shared_from && file.message_sequence == null && (
            <button
              className="text-xs text-zinc-400 hover:text-red-600"
              type="button"
              aria-label={`Delete ${label}`}
              title="Delete"
              onClick={onDelete}
            >
              ✕
            </button>
          )}
        </div>
        <div className="truncate text-xs text-zinc-500" title={file.shared_from?.label}>
          {where} · {size(file.size)} · {when(file.shared_from?.shared_at ?? file.updated_at)}
          {file.archived_at && ' · archived'}
        </div>
      </div>
    </div>
  )
}
