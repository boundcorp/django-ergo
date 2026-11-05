import { describe, expect, test } from 'bun:test'
import { followsChat } from './Pins'

describe('open pins and new chat messages', () => {
  test('pages and docs reload with the chat', () => {
    expect(followsChat({ kind: 'bot_file', name: 'Board', url: '/b', path: 'pages/board.jhtml' })).toBe(true)
    expect(followsChat({ kind: 'file', name: 'notes.md', url: '/f', view: 'markdown' })).toBe(true)
  })

  test('audio and video keep playing', () => {
    expect(followsChat({ kind: 'file', name: 'clip.mp4', url: '/f' })).toBe(false)
    expect(followsChat({ kind: 'file', name: 'Recording', url: '/f', view: 'media' })).toBe(false)
    expect(followsChat({ kind: 'bot_file', name: 'Intro', url: '/b', path: 'media/intro.webm' })).toBe(false)
    expect(followsChat({ kind: 'file', name: 'My take', url: '/f', filename: 'take.MP3' })).toBe(false)
  })
})
