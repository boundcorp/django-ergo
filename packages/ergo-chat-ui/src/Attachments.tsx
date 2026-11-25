import { formatBytes } from './format'
import type { Attachment } from './types'
import { safeAttachmentUrl } from './url'

function kindOf(mediaType: string | undefined): 'image' | 'video' | 'audio' | 'file' {
  const major = mediaType?.split('/')[0]?.toLowerCase()
  return major === 'image' || major === 'video' || major === 'audio' ? major : 'file'
}

/** One attachment: images, video and audio inline, anything else as a file chip. Unsafe URLs render as plain text. */
export function AttachmentView({ attachment }: { attachment: Attachment }) {
  const url = safeAttachmentUrl(attachment.url)
  const kind = kindOf(attachment.mediaType)
  const meta = attachment.size != null ? ` (${formatBytes(attachment.size)})` : ''
  if (url && kind === 'image')
    return (
      <a className="ergo-chat__media" href={url} target="_blank" rel="noopener noreferrer">
        <img src={url} alt={attachment.name} loading="lazy" referrerPolicy="no-referrer" />
      </a>
    )
  if (url && kind === 'video')
    return <video className="ergo-chat__media" src={url} controls preload="metadata" aria-label={attachment.name} />
  if (url && kind === 'audio')
    return <audio className="ergo-chat__audio" src={url} controls preload="metadata" aria-label={attachment.name} />
  return url ? (
    <a className="ergo-chat__file" href={url} target="_blank" rel="noopener noreferrer" download={attachment.name}>
      <span aria-hidden>📎</span> {attachment.name}
      {meta}
    </a>
  ) : (
    <span className="ergo-chat__file">
      <span aria-hidden>📎</span> {attachment.name}
      {meta}
    </span>
  )
}

export function AttachmentList({ attachments }: { attachments: Attachment[] }) {
  if (!attachments.length) return null
  return (
    <div className="ergo-chat__attachments">
      {attachments.map(a => (
        <AttachmentView key={a.id} attachment={a} />
      ))}
    </div>
  )
}
