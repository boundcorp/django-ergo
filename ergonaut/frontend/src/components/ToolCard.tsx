import { useState } from 'react'
import type { Block } from '../api'

type ToolUse = Extract<Block, { type: 'tool_use' }>
type ToolResult = Extract<Block, { type: 'tool_result' }>

export function pretty(value: unknown): string {
  if (typeof value === 'string') return value
  return JSON.stringify(value, null, 2)
}

export function resultText(content: unknown): string {
  if (Array.isArray(content)) {
    return content.map(part => (typeof part === 'object' && part && 'text' in part ? String(part.text) : pretty(part))).join('\n')
  }
  return pretty(content)
}

function summarize(input: unknown): string {
  if (!input || typeof input !== 'object') return ''
  const text = Object.entries(input as Record<string, unknown>)
    .map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
    .join(', ')
  return text.length > 80 ? `${text.slice(0, 77)}…` : text
}

export function ToolCard({ use, result, pending }: { use: ToolUse; result?: ToolResult; pending?: boolean }) {
  const [open, setOpen] = useState(false)
  const state = pending ? 'waiting for approval' : !result ? 'no result' : result.is_error ? 'error' : 'ok'
  const tone = pending
    ? 'border-amber-400/60 bg-amber-50 dark:bg-amber-950/30'
    : result?.is_error
      ? 'border-red-400/60 bg-red-50 dark:bg-red-950/30'
      : 'border-zinc-200 bg-zinc-50 dark:border-zinc-800 dark:bg-zinc-900'
  return (
    <div className={`my-1 rounded-lg border text-sm ${tone}`}>
      <button
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
      >
        <span className="font-mono text-xs text-zinc-500">{open ? '▾' : '▸'}</span>
        <span className="font-mono font-medium">{use.name}</span>
        <span className="truncate font-mono text-xs text-zinc-500">{summarize(use.input)}</span>
        <span className="ml-auto shrink-0 text-xs text-zinc-500">{state}</span>
      </button>
      {open && (
        <div className="space-y-2 border-t border-inherit px-3 py-2">
          <div>
            <div className="mb-1 text-xs font-medium text-zinc-500">Arguments</div>
            <pre className="overflow-x-auto whitespace-pre-wrap break-words font-mono text-xs">{pretty(use.input)}</pre>
          </div>
          {result && (
            <div>
              <div className="mb-1 text-xs font-medium text-zinc-500">Result</div>
              <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words font-mono text-xs">
                {resultText(result.content)}
              </pre>
            </div>
          )}
          <div className="font-mono text-[11px] text-zinc-400">{use.id}</div>
        </div>
      )}
    </div>
  )
}
