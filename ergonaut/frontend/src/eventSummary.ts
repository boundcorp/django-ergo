/** Long automated event messages (worker results, failures) show a short
 * summary in the chat, with the full text behind a disclosure. */

const SHORT = 600
const SUMMARY_CHARS = 400

/** The `summary` field of a worker's JSON result, even when the JSON was cut short. */
function resultSummary(body: string): string | null {
  try {
    const parsed = JSON.parse(body)
    if (parsed && typeof parsed === 'object' && typeof parsed.summary === 'string') return parsed.summary
    if (typeof parsed === 'string') return parsed
  } catch {
    const match = body.match(/"summary"\s*:\s*("(?:[^"\\]|\\.)*")/)
    if (match) {
      try {
        return JSON.parse(match[1]) as string
      } catch {
        return null
      }
    }
  }
  return null
}

/** The first paragraph, clipped at a word boundary. */
function firstParagraph(text: string): string {
  const para = text
    .trim()
    .split(/\n\s*\n/)[0]
    .trim()
  if (para.length <= SUMMARY_CHARS) return para
  const cut = para.slice(0, SUMMARY_CHARS)
  const space = cut.lastIndexOf(' ')
  return `${(space > SUMMARY_CHARS / 2 ? cut.slice(0, space) : cut).trimEnd()}…`
}

/** The short form of an event's text, or null when it is short enough to show whole. */
export function eventSummary(text: string): string | null {
  if (text.length <= SHORT) return null
  const marker = text.indexOf('\n\nResult:\n')
  if (marker >= 0) {
    const head = text.slice(0, marker).trim()
    const body = text.slice(marker + '\n\nResult:\n'.length)
    const summary = resultSummary(body.trim()) ?? body
    return `${head}\n\n${firstParagraph(summary)}`
  }
  const [head, ...rest] = text.trim().split(/\n\s*\n/)
  const short = firstParagraph(head)
  if (short !== head.trim() || !rest.length) return short
  return `${short}\n\n${firstParagraph(rest.join('\n\n'))}`
}
