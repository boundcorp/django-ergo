// Run with `npm test` (Node's built-in runner; no extra dependency).
import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  argsPreview,
  formatArgs,
  rawArgs,
  resultSummary,
  resultText,
  toolNameParts,
  type ArgValue,
} from './toolFormat.ts'

function entries(input: unknown): Record<string, ArgValue> {
  const args = formatArgs(input)
  assert.equal(args.kind, 'entries')
  return args.kind === 'entries' ? Object.fromEntries(args.entries.map(e => [e.key, e.value])) : {}
}

test('mcp tool names split into server and tool; other names are left alone', () => {
  assert.deepEqual(toolNameParts('mcp__orca__orca_start_worker'), { server: 'orca', tool: 'orca_start_worker' })
  assert.deepEqual(toolNameParts('mcp__github__list__issues'), { server: 'github', tool: 'list__issues' })
  assert.deepEqual(toolNameParts('Bash'), { server: null, tool: 'Bash' })
  assert.deepEqual(toolNameParts('mcp__lonely'), { server: null, tool: 'mcp__lonely' })
})

test('short strings are inline; paths and code are monospace, prose is not', () => {
  const value = entries({ file_path: '/tmp/a b/c.py', note: 'fix the bug', id: 'abc-123', command: 'ls -la' })
  assert.deepEqual(value.file_path, { kind: 'text', text: '/tmp/a b/c.py', mono: true, long: false })
  assert.deepEqual(value.note, { kind: 'text', text: 'fix the bug', mono: false, long: false })
  assert.deepEqual(value.id, { kind: 'text', text: 'abc-123', mono: true, long: false })
  assert.deepEqual(value.command, { kind: 'text', text: 'ls -la', mono: true, long: false })
})

test('long and multiline strings are marked for clamping', () => {
  const value = entries({ prose: 'word '.repeat(60), code: 'a\nb\nc\nd', two: 'a\nb' })
  const prose = 'word '.repeat(60)
  assert.deepEqual(value.prose, { kind: 'text', text: prose, mono: false, long: true })
  assert.deepEqual(value.code, { kind: 'text', text: 'a\nb\nc\nd', mono: true, long: true })
  assert.deepEqual(value.two, { kind: 'text', text: 'a\nb', mono: true, long: false })
})

test('scalars, short arrays and nested values', () => {
  const value = entries({
    n: 3,
    ok: false,
    nothing: null,
    tags: ['a', 'b', 1],
    empty: [],
    nested: { a: [1, { b: 2 }] },
  })
  assert.deepEqual(value.n, { kind: 'scalar', text: '3' })
  assert.deepEqual(value.ok, { kind: 'scalar', text: 'false' })
  assert.deepEqual(value.nothing, { kind: 'scalar', text: 'null' })
  assert.deepEqual(value.tags, { kind: 'chips', items: ['a', 'b', '1'] })
  assert.deepEqual(value.empty, { kind: 'json', text: '[]', long: false })
  assert.deepEqual(value.nested, { kind: 'json', text: JSON.stringify({ a: [1, { b: 2 }] }, null, 2), long: false })
})

test('arrays that are too long, hold long strings or hold objects are not chips', () => {
  const value = entries({
    many: Array.from({ length: 9 }, (_, i) => String(i)),
    wide: ['x'.repeat(41)],
    objects: [{ a: 1 }],
  })
  for (const key of ['many', 'wide', 'objects']) assert.equal(value[key].kind, 'json')
})

test('a JSON string input (openai_api, codex_cli) is parsed like an object', () => {
  const value = entries('{"command": ["bash", "-lc", "ls"]}')
  assert.deepEqual(value.command, { kind: 'chips', items: ['bash', '-lc', 'ls'] })
})

test('invalid JSON, plain text and non-object input fall back to raw text', () => {
  assert.deepEqual(formatArgs('{"a": '), { kind: 'raw', text: '{"a": ' })
  assert.deepEqual(formatArgs('ls -la'), { kind: 'raw', text: 'ls -la' })
  assert.deepEqual(formatArgs('[1,2]'), { kind: 'raw', text: '[1,2]' })
  assert.deepEqual(formatArgs(null), { kind: 'raw', text: 'null' })
  assert.deepEqual(formatArgs(undefined), { kind: 'raw', text: 'undefined' })
  assert.deepEqual(formatArgs([1, 2]), { kind: 'raw', text: '[\n  1,\n  2\n]' })
})

test('an empty object has no entries', () => {
  assert.deepEqual(formatArgs({}), { kind: 'entries', entries: [] })
})

test('rawArgs keeps every key and value; broken JSON is returned untouched', () => {
  const input = { a: 'x'.repeat(500), b: { c: [1, 2] } }
  assert.deepEqual(JSON.parse(rawArgs(input)), input)
  assert.equal(rawArgs('{"a":1}'), '{\n  "a": 1\n}')
  assert.equal(rawArgs('{"a":'), '{"a":')
})

test('args preview is one line, truncated, and handles every shape', () => {
  assert.equal(argsPreview({ command: 'ls -la', tags: ['a', 'b'] }), 'command=ls -la, tags=a b')
  assert.equal(argsPreview({ text: 'line one\nline two' }), 'text=line one line two')
  const long = argsPreview({ text: 'x'.repeat(300) })
  assert.equal(long.length, 80)
  assert.ok(long.endsWith('…'))
  assert.equal(argsPreview('{"a":1}'), 'a=1')
  assert.equal(argsPreview('not json'), 'not json')
  assert.equal(argsPreview({}), '')
  assert.equal(argsPreview(undefined), 'undefined')
})

test('result summary counts lines and size and previews the first line', () => {
  assert.deepEqual(resultSummary('hello'), { size: '1 line · 5 chars', preview: 'hello' })
  assert.deepEqual(resultSummary('a\nb\nc\n'), { size: '3 lines · 6 chars', preview: 'a b c' })
  assert.deepEqual(resultSummary(''), { size: 'empty', preview: '' })
  assert.equal(resultSummary('x'.repeat(2048)).size, '1 line · 2.0 KB')
})

test('result text joins text parts and keeps other parts as JSON', () => {
  const content = [
    { type: 'text', text: 'one' },
    { type: 'image_ref', attachment_id: 'a1' },
  ]
  const text = resultText(content)
  assert.ok(text.startsWith('one\n'))
  assert.ok(text.includes('"attachment_id": "a1"'))
  assert.equal(resultText({ a: 1 }), '{\n  "a": 1\n}')
})
