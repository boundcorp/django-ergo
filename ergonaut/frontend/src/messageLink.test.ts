import { describe, expect, it } from 'bun:test'
import { linkedLine, messageAnchor, messageLink } from './messageLink'

describe('message links', () => {
  it('builds a link to one message', () => {
    expect(messageAnchor(12)).toBe('m-12')
    expect(messageLink('abc', 12, 'https://ergo.example.com')).toBe('https://ergo.example.com/s/abc#m-12')
  })

  it('reads the line back from the hash', () => {
    expect(linkedLine('#m-12')).toBe(12)
    expect(linkedLine('#m-0')).toBe(0)
    expect(linkedLine('')).toBeNull()
    expect(linkedLine('#m-')).toBeNull()
    expect(linkedLine('#other')).toBeNull()
  })
})
