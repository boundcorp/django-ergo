// Pure helpers for showing a tool call: no React, so they can be tested with `npm test`.

export function pretty(value: unknown): string {
  if (typeof value === 'string') return value
  return JSON.stringify(value, null, 2) ?? String(value)
}

export function resultText(content: unknown): string {
  if (Array.isArray(content)) {
    return content
      .map(part => (typeof part === 'object' && part && 'text' in part ? String(part.text) : pretty(part)))
      .join('\n')
  }
  return pretty(content)
}

/** `mcp__server__tool` -> {server, tool}; any other name has no server. */
export function toolNameParts(name: string): { server: string | null; tool: string } {
  const match = /^mcp__(.+?)__(.+)$/.exec(name)
  return match ? { server: match[1], tool: match[2] } : { server: null, tool: name }
}

// A string longer than this, or with more lines, is clamped behind "show more".
export const LONG_CHARS = 160
export const LONG_LINES = 3
// An array of at most this many short scalars is shown as chips.
const CHIP_MAX_ITEMS = 8
const CHIP_MAX_CHARS = 40

export type ArgValue =
  | { kind: 'text'; text: string; mono: boolean; long: boolean } // a string; long ones are clamped
  | { kind: 'scalar'; text: string } // number, boolean, null
  | { kind: 'chips'; items: string[] } // a short array of scalars
  | { kind: 'json'; text: string; long: boolean } // a nested object or longer array, as indented JSON

export type ArgEntry = { key: string; value: ArgValue }

export type Args = { kind: 'entries'; entries: ArgEntry[] } | { kind: 'raw'; text: string } // not an object (or not JSON): shown as it came

// Keys whose values are code, paths or ids: shown in monospace even when they contain spaces.
const MONO_KEY =
  /(^|_|-)(path|paths|file|files|dir|cwd|command|cmd|code|script|query|sql|url|uri|id|ids|pattern|regex)$/i

function isScalar(value: unknown): boolean {
  return value === null || ['string', 'number', 'boolean'].includes(typeof value)
}

export function formatValue(key: string, value: unknown): ArgValue {
  if (typeof value === 'string') {
    // A string without spaces is a path, id, flag or the like; so is anything spanning lines (code).
    const mono = MONO_KEY.test(key) || !/\s/.test(value) || value.includes('\n')
    const long = value.length > LONG_CHARS || value.split('\n').length > LONG_LINES
    return { kind: 'text', text: value, mono, long }
  }
  if (isScalar(value)) return { kind: 'scalar', text: String(value) }
  if (
    Array.isArray(value) &&
    value.length > 0 &&
    value.length <= CHIP_MAX_ITEMS &&
    value.every(v => isScalar(v) && String(v).length <= CHIP_MAX_CHARS && !String(v).includes('\n'))
  )
    return { kind: 'chips', items: value.map(String) }
  const text = JSON.stringify(value, null, 2) ?? String(value)
  return { kind: 'json', text, long: text.split('\n').length > 12 }
}

/** A tool call's input as key/value entries. The engines differ in what they store: an object
 *  (claude_code, claude_api, MCP), a JSON string (openai_api, codex_cli), or plain text.
 *  Anything that is not a JSON object falls back to raw text. */
export function formatArgs(input: unknown): Args {
  let value = input
  if (typeof value === 'string') {
    try {
      value = JSON.parse(value)
    } catch {
      return { kind: 'raw', text: input as string }
    }
  }
  if (value && typeof value === 'object' && !Array.isArray(value))
    return {
      kind: 'entries',
      entries: Object.entries(value as Record<string, unknown>).map(([key, v]) => ({
        key,
        value: formatValue(key, v),
      })),
    }
  return { kind: 'raw', text: typeof input === 'string' ? input : pretty(input) }
}

/** The full input for the "raw JSON" view and the copy button: JSON as stored, indented. */
export function rawArgs(input: unknown): string {
  if (typeof input === 'string') {
    try {
      return JSON.stringify(JSON.parse(input), null, 2)
    } catch {
      return input
    }
  }
  return pretty(input)
}

function oneLine(text: string, max: number): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat
}

/** `command=ls -la, path=/tmp`: a short preview of the args for the call's one-line header. */
export function argsPreview(input: unknown, max = 80): string {
  const args = formatArgs(input)
  if (args.kind === 'raw') return oneLine(args.text, max)
  const text = args.entries
    .map(({ key, value }) => {
      const shown = value.kind === 'chips' ? value.items.join(' ') : value.text
      return `${key}=${shown}`
    })
    .join(', ')
  return oneLine(text, max)
}

/** "12 lines · 3.4 KB" and the first line of the result, for the folded result row. */
export function resultSummary(content: unknown): { size: string; preview: string } {
  const text = resultText(content)
  if (!text.trim()) return { size: 'empty', preview: '' }
  const lines = text.replace(/\n+$/, '').split('\n').length
  const size = text.length < 1024 ? `${text.length} chars` : `${(text.length / 1024).toFixed(1)} KB`
  return { size: `${lines} line${lines > 1 ? 's' : ''} · ${size}`, preview: oneLine(text, 80) }
}
