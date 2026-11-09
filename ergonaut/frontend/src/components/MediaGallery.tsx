import { useCallback, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import type { AttachmentFile } from '../api'
import { api } from '../api'
import { PrChip } from './Threads'

/** A file to show in the chat: sent with a message, made by the bot, or attached to a reply. */
export type MediaItem = {
  id?: string
  label: string
  kind?: string
  media_type?: string
  note?: string
  link?: AttachmentFile['link']
  image?: boolean // known to be an image (a tool's image_ref)
  audio?: boolean
}

export type MediaKind = 'image' | 'video' | 'audio' | 'other'

const IMAGE_EXT = /\.(png|jpe?g|gif|webp|svg|avif)$/i
const VIDEO_EXT = /\.(mp4|m4v|mov|webm|mkv|ogv)$/i
const AUDIO_EXT = /\.(mp3|m4a|wav|ogg|oga|opus|flac|aac)$/i

/** What a file plays as, from its kind, media type or file name. */
export function mediaKind(file: { kind?: string; media_type?: string; label?: string; filename?: string }): MediaKind {
  const type = file.media_type ?? ''
  const name = file.filename ?? file.label ?? ''
  // The stored kind first: a voice note recorded as video/webm is still audio.
  if (file.kind === 'image' || file.kind === 'audio' || file.kind === 'video') return file.kind
  if (type.startsWith('image/')) return 'image'
  if (type.startsWith('video/')) return 'video'
  if (type.startsWith('audio/')) return 'audio'
  if (type && type !== 'application/octet-stream') return 'other'
  if (IMAGE_EXT.test(name)) return 'image'
  if (VIDEO_EXT.test(name)) return 'video'
  if (AUDIO_EXT.test(name)) return 'audio'
  return 'other'
}

/** Whether an attachment is audio, from its kind or media type. */
export function isAudio(file: { kind?: string; media_type?: string }): boolean {
  return mediaKind(file) === 'audio'
}

function kindOf(item: MediaItem): MediaKind {
  if (item.image) return 'image'
  if (item.audio) return 'audio'
  return mediaKind(item)
}

const thumbUrl = (id: string) => `/api/attachments/${id}/thumbnail`

/** Files a bot attached to a reply, by id or filename (the newest file of that name). */
export function resolveFiles(refs: string[], files: AttachmentFile[]): MediaItem[] {
  return refs.map(ref => {
    const file =
      files.find(f => f.id === ref) ??
      [...files].sort((a, b) => b.created_at.localeCompare(a.created_at)).find(f => f.filename === ref)
    if (!file) return { label: ref, note: 'not found in this chat' }
    return {
      id: file.id,
      label: file.filename || file.media_type,
      kind: file.kind,
      media_type: file.media_type,
      link: file.link,
    }
  })
}

/**
 * Several files at once: images and videos as a grid that opens into a slideshow,
 * audio and other files below it. One picture or clip shows on its own.
 */
export function MediaGallery({ items }: { items: MediaItem[] }) {
  const [open, setOpen] = useState<number | null>(null)
  const visual = items.filter(item => item.id && (kindOf(item) === 'image' || kindOf(item) === 'video'))
  const rest = items.filter(item => !visual.includes(item))
  const shown = visual.length > 6 ? visual.slice(0, 6) : visual
  return (
    <div className="flex min-w-0 max-w-full flex-col items-start gap-2">
      {visual.length === 1 && <AttachmentView {...visual[0]} onOpen={() => setOpen(0)} />}
      {visual.length > 1 && (
        <div
          className={`grid w-[28rem] max-w-full gap-1 ${visual.length === 2 || visual.length === 4 ? 'grid-cols-2' : 'grid-cols-3'}`}
          role="group"
          aria-label={`${visual.length} files`}
        >
          {shown.map((item, i) => (
            <button
              key={item.id}
              type="button"
              className="group relative aspect-square overflow-hidden rounded-lg border border-zinc-300 bg-zinc-100 dark:border-zinc-700 dark:bg-zinc-900"
              title={item.label}
              aria-label={`Open ${item.label}`}
              onClick={() => setOpen(i)}
            >
              <img
                src={kindOf(item) === 'video' ? thumbUrl(item.id!) : api.viewUrl(item.id!)}
                alt=""
                loading="lazy"
                className="h-full w-full object-cover transition group-hover:scale-105"
                onError={e => (e.currentTarget.style.visibility = 'hidden')}
              />
              {kindOf(item) === 'video' && (
                <span className="absolute inset-0 flex items-center justify-center">
                  <span className="rounded-full bg-black/60 px-2.5 py-1 text-lg text-white">▶</span>
                </span>
              )}
              {i === shown.length - 1 && visual.length > shown.length && (
                <span className="absolute inset-0 flex items-center justify-center bg-black/55 text-xl font-semibold text-white">
                  +{visual.length - shown.length}
                </span>
              )}
            </button>
          ))}
        </div>
      )}
      {!!rest.length && (
        <div className="flex min-w-0 max-w-full flex-wrap gap-2">
          {rest.map((item, i) => (
            <AttachmentView key={item.id ?? `${item.label}-${i}`} {...item} />
          ))}
        </div>
      )}
      {open != null && <Lightbox items={visual} start={open} onClose={() => setOpen(null)} />}
    </div>
  )
}

/** A full-screen slideshow: arrows or swipe to page, Escape to close. */
export function Lightbox({ items, start, onClose }: { items: MediaItem[]; start: number; onClose: () => void }) {
  const [index, setIndex] = useState(start)
  const [touch, setTouch] = useState<number | null>(null)
  const item = items[index]
  const step = useCallback((by: number) => setIndex(i => (i + by + items.length) % items.length), [items.length])
  useEffect(() => {
    function key(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
      else if (e.key === 'ArrowRight') step(1)
      else if (e.key === 'ArrowLeft') step(-1)
    }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  }, [onClose, step])
  if (!item?.id) return null
  const url = api.viewUrl(item.id)
  const many = items.length > 1
  // On the body, so no transformed or clipped ancestor in the transcript can box it in.
  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label={item.label}
      className="fixed inset-0 z-50 flex flex-col bg-black/95 text-white"
      onClick={e => e.target === e.currentTarget && onClose()}
      onTouchStart={e => setTouch(e.touches[0].clientX)}
      onTouchEnd={e => {
        if (touch == null || !many) return
        const dx = e.changedTouches[0].clientX - touch
        if (Math.abs(dx) > 50) step(dx < 0 ? 1 : -1)
        setTouch(null)
      }}
    >
      <div className="flex items-center gap-3 px-4 py-3 text-sm">
        {many && (
          <span className="tabular-nums opacity-70">
            {index + 1} / {items.length}
          </span>
        )}
        <span className="min-w-0 truncate">{item.label}</span>
        <a
          href={api.downloadUrl(item.id)}
          className="ml-auto rounded-md px-2 py-1 hover:bg-white/10"
          aria-label={`Download ${item.label}`}
          title="Download"
        >
          ⬇
        </a>
        <button type="button" className="rounded-md px-2 py-1 hover:bg-white/10" aria-label="Close" onClick={onClose}>
          ✕
        </button>
      </div>
      <div
        className="relative flex min-h-0 flex-1 items-center justify-center px-2 pb-4 sm:px-16"
        onClick={e => e.target === e.currentTarget && onClose()}
      >
        {kindOf(item) === 'video' ? (
          <video
            key={item.id}
            src={url}
            controls
            autoPlay
            playsInline
            poster={thumbUrl(item.id)}
            className="max-h-full max-w-full rounded-lg"
          />
        ) : (
          <img key={item.id} src={url} alt={item.label} className="max-h-full max-w-full rounded-lg object-contain" />
        )}
        {many && (
          <>
            <button
              type="button"
              aria-label="Previous"
              className="absolute left-1 top-1/2 -translate-y-1/2 rounded-full bg-white/10 px-3 py-2 text-2xl hover:bg-white/20 sm:left-4"
              onClick={() => step(-1)}
            >
              ‹
            </button>
            <button
              type="button"
              aria-label="Next"
              className="absolute right-1 top-1/2 -translate-y-1/2 rounded-full bg-white/10 px-3 py-2 text-2xl hover:bg-white/20 sm:right-4"
              onClick={() => step(1)}
            >
              ›
            </button>
          </>
        )}
      </div>
    </div>,
    document.body,
  )
}

/** One file in the chat: an image, a video or audio player, a pull request, or a link. */
export function AttachmentView(props: MediaItem & { onOpen?: () => void }) {
  const { id, label, note, link, onOpen } = props
  const [open, setOpen] = useState(false)
  if (link) return <PrChip pr={link} />
  const kind = kindOf(props)
  const title = note ? `${label} · ${note}` : label
  if (id && kind === 'audio')
    return (
      <div className="flex w-80 max-w-full flex-col gap-1 rounded-lg border border-zinc-300 p-2 dark:border-zinc-700">
        <a
          href={api.viewUrl(id)}
          target="_blank"
          rel="noreferrer"
          title={note}
          className="truncate text-xs text-indigo-600 hover:underline dark:text-indigo-400"
        >
          🔊 {label}
        </a>
        <audio controls preload="metadata" src={api.viewUrl(id)} aria-label={label} className="h-9 w-full" />
      </div>
    )
  if (id && kind === 'video')
    return (
      <figure className="flex w-[28rem] max-w-full flex-col gap-1 overflow-hidden rounded-lg border border-zinc-300 bg-black dark:border-zinc-700">
        {/* #t=0.1 shows a first frame where the server has no preview (no ffmpeg). */}
        <video
          controls
          playsInline
          preload="metadata"
          poster={thumbUrl(id)}
          src={`${api.viewUrl(id)}#t=0.1`}
          aria-label={label}
          className="max-h-80 w-full bg-black"
        />
        <figcaption className="flex items-center gap-2 bg-white px-2 py-1 text-xs dark:bg-zinc-950">
          <span className="truncate" title={title}>
            🎬 {label}
          </span>
          <a
            href={api.downloadUrl(id)}
            className="ml-auto text-zinc-400 hover:text-zinc-700 dark:hover:text-zinc-100"
            aria-label={`Download ${label}`}
            title="Download"
          >
            ⬇
          </a>
        </figcaption>
      </figure>
    )
  if (id && kind === 'image')
    return (
      <>
        <button
          type="button"
          className="block max-w-full"
          title={title}
          aria-label={`Open ${label}`}
          onClick={() => (onOpen ? onOpen() : setOpen(true))}
        >
          <img
            src={api.viewUrl(id)}
            alt={label}
            loading="lazy"
            className="max-h-60 max-w-full rounded-lg border border-zinc-300 dark:border-zinc-700"
          />
        </button>
        {open && <Lightbox items={[props]} start={0} onClose={() => setOpen(false)} />}
      </>
    )
  return id ? (
    <a
      href={api.viewUrl(id)}
      target="_blank"
      rel="noreferrer"
      title={note}
      className="rounded-md border border-zinc-300 px-2 py-1 text-xs text-indigo-600 hover:underline dark:border-zinc-700 dark:text-indigo-400"
    >
      📎 {label}
    </a>
  ) : (
    <div
      className="rounded-md border border-zinc-300 px-2 py-1 text-xs text-zinc-500 dark:border-zinc-700"
      title={note}
    >
      📎 {label}
      {note && <span className="opacity-70"> · {note}</span>}
    </div>
  )
}
