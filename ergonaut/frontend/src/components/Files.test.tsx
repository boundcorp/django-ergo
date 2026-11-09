import { describe, expect, test } from 'bun:test'
import type { AttachmentFile } from '../api'
import { fileCategory, filterFiles } from './Files'

function file(over: Partial<AttachmentFile>): AttachmentFile {
  return {
    id: over.filename ?? 'f',
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
  }
}

describe('the Files panel', () => {
  test('sorts files into images, audio and video, and docs', () => {
    expect(fileCategory(file({ view: 'image', kind: 'image', media_type: 'image/png' }))).toBe('image')
    expect(fileCategory(file({ view: 'media', kind: 'audio', media_type: 'audio/webm' }))).toBe('media')
    expect(fileCategory(file({ view: '', kind: 'video', media_type: 'video/quicktime' }))).toBe('media')
    expect(fileCategory(file({ view: 'markdown', media_type: 'text/markdown' }))).toBe('doc')
    expect(
      fileCategory(file({ view: '', link: { id: 'x', url: 'u', repo: 'r', number: 1, title: 't' } as never })),
    ).toBe('link')
  })

  test('filters by type, words and archive', () => {
    const files = [
      file({ filename: 'mock.png', view: 'image', kind: 'image', media_type: 'image/png' }),
      file({ filename: 'todo.md', view: 'markdown', media_type: 'text/markdown' }),
      file({
        filename: 'roadmap-recovery.md',
        view: 'markdown',
        media_type: 'text/markdown',
        shared_from: { session_id: 's', label: 'devbox · Storytime roadmap', message_id: 'm', shared_at: '' },
      }),
      file({ filename: 'old.md', view: 'markdown', archived_at: '2026-11-01T00:00:00Z' }),
    ]
    const names = (list: AttachmentFile[]) => list.map(f => f.filename)
    expect(names(filterFiles(files, 'all', '', false))).toEqual(['mock.png', 'todo.md', 'roadmap-recovery.md'])
    expect(names(filterFiles(files, 'image', '', false))).toEqual(['mock.png'])
    expect(names(filterFiles(files, 'doc', 'storytime', false))).toEqual(['roadmap-recovery.md'])
    expect(names(filterFiles(files, 'doc', 'MD', true))).toEqual(['todo.md', 'roadmap-recovery.md', 'old.md'])
  })
})
