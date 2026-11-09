import { describe, expect, test } from 'bun:test'
import type { AttachmentFile } from '../api'
import { mediaKind, resolveFiles } from './MediaGallery'

const file = (over: Partial<AttachmentFile>): AttachmentFile => ({
  id: 'f',
  filename: 'f',
  media_type: 'text/plain',
  kind: 'document',
  size: 1,
  source: 'bot',
  message_sequence: null,
  pinned: false,
  view: 'text',
  created_at: '2026-11-09T00:00:00Z',
  updated_at: '2026-11-09T00:00:00Z',
  ...over,
})

describe('files in the chat', () => {
  test('play as images, videos or audio', () => {
    expect(mediaKind({ kind: 'image', media_type: 'image/png' })).toBe('image')
    expect(mediaKind({ kind: 'document', media_type: 'video/mp4' })).toBe('video')
    // A voice note recorded as webm video is still audio.
    expect(mediaKind({ kind: 'audio', media_type: 'video/webm' })).toBe('audio')
    expect(mediaKind({ media_type: 'audio/mpeg' })).toBe('audio')
    expect(mediaKind({ label: 'clip.MOV' })).toBe('video')
    expect(mediaKind({ label: 'render.png', media_type: 'application/octet-stream' })).toBe('image')
    expect(mediaKind({ kind: 'document', media_type: 'application/pdf' })).toBe('other')
  })

  test("a reply's attachments are found by id or by the newest file of that name", () => {
    const files = [
      file({ id: 'old', filename: 'board.png', created_at: '2026-11-01T00:00:00Z' }),
      file({ id: 'new', filename: 'board.png', created_at: '2026-11-09T00:00:00Z' }),
      file({ id: 'clip', filename: 'clip.mp4', media_type: 'video/mp4' }),
    ]
    const found = resolveFiles(['board.png', 'clip', 'missing.png'], files)
    expect(found.map(f => f.id)).toEqual(['new', 'clip', undefined])
    expect(found[2].note).toBe('not found in this chat')
  })
})
