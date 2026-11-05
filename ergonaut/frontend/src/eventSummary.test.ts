import { describe, expect, it } from 'bun:test'
import { eventSummary } from './eventSummary'

const longSummary = `I ran both catalog checks and they returned HTTP 200.\n\n${'Preset voices are listed here. '.repeat(40)}`

describe('event summaries', () => {
  it('leaves short events whole', () => {
    expect(eventSummary('Worker “x” finished.\n\nResult:\nok')).toBeNull()
  })

  it('shows the first paragraph of a worker result summary', () => {
    const result = { job: 'j', summary: longSummary, outputs: [{ path: 'out/a.md', sha256: 'f'.repeat(64) }] }
    const text = `Worker “Catalog” finished.\n\nResult:\n${JSON.stringify(result, null, 2)}`
    expect(eventSummary(text)).toBe(
      'Worker “Catalog” finished.\n\nI ran both catalog checks and they returned HTTP 200.',
    )
  })

  it('reads the summary from a result that was cut short', () => {
    const json = JSON.stringify({ summary: 'Short answer.\n\nMore.', outputs: ['x'.repeat(800)] })
    const text = `Worker “Cut” finished.\n\nResult:\n${json.slice(0, 700)}`
    expect(eventSummary(text)).toBe('Worker “Cut” finished.\n\nShort answer.')
  })

  it('clips a plain text result', () => {
    const text = `Worker “Plain” finished.\n\nResult:\n${'word '.repeat(200)}`
    const summary = eventSummary(text)!
    expect(summary.startsWith('Worker “Plain” finished.\n\nword word')).toBe(true)
    expect(summary.endsWith('…')).toBe(true)
    expect(summary.length).toBeLessThan(450)
  })

  it('clips other long events', () => {
    const summary = eventSummary(`Worker “W” failed: ${'boom '.repeat(200)}`)!
    expect(summary.endsWith('…')).toBe(true)
    expect(summary.length).toBeLessThanOrEqual(401)
  })
})
