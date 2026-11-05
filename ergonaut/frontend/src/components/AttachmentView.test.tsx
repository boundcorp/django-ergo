import { describe, expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { AttachmentView, isAudio } from './Transcript'

describe('audio attachments', () => {
  test('are recognised by kind or media type', () => {
    expect(isAudio({ kind: 'audio' })).toBe(true)
    expect(isAudio({ media_type: 'audio/mpeg' })).toBe(true)
    expect(isAudio({ kind: 'document', media_type: 'text/csv' })).toBe(false)
  })

  test('render an inline player with a link to the file', () => {
    const html = renderToStaticMarkup(createElement(AttachmentView, { id: 'a1', label: 'clip.mp3', audio: true }))
    expect(html).toContain('<audio')
    expect(html).toContain('src="/api/attachments/a1/download?inline=true"')
    expect(html).toContain('clip.mp3')
  })

  test('without a stored file stay a chip', () => {
    const html = renderToStaticMarkup(createElement(AttachmentView, { label: '[audio attachment]', audio: true }))
    expect(html).not.toContain('<audio')
  })
})
