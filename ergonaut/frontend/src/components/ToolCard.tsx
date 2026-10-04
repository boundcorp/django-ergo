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
    return content
      .map(part => (typeof part === 'object' && part && 'text' in part ? String(part.text) : pretty(part)))
      .join('\n')
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

function seconds(ms: number): string {
  if (ms < 1000) return `${Math.max(0, Math.round(ms))}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)}s`
  return `${Math.floor(ms / 60_000)}m${Math.round((ms % 60_000) / 1000)}s`
}

/** A tool call as one line (status, name, arguments, time); the arguments and result open on click. */
export function ToolCard({
  use,
  result,
  pending,
  duration,
}: {
  use: ToolUse
  result?: ToolResult
  pending?: boolean
  duration?: number | null // ms from the call to its result
}) {
  const [open, setOpen] = useState(false)
  const [icon, state, tone] = pending
    ? ['⏸', 'needs approval', 'border-warning/50 bg-amber-tint']
    : !result
      ? ['…', 'running', 'border-stroke bg-surface']
      : result.is_error
        ? ['✗', 'error', 'border-danger/50 bg-red-tint']
        : ['✓', 'ok', 'border-stroke bg-surface']
  return (
    <div className={`rounded-control border text-xs ${tone}`}>
      <button
        className="flex w-full items-center gap-2 px-2.5 py-1 text-left"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        title={state}
      >
        <span
          className={`w-3 shrink-0 text-center ${result?.is_error ? 'text-danger' : pending ? 'text-warning' : result ? 'text-success' : 'animate-pulse text-accent'}`}
        >
          {icon}
        </span>
        <span className="shrink-0 font-mono font-medium">{use.name}</span>
        <span className="min-w-0 truncate font-mono text-zinc-500">{summarize(use.input)}</span>
        <span className="ml-auto shrink-0 text-zinc-500">
          {duration != null && result ? seconds(duration) : pending ? state : ''}
        </span>
        <span className="shrink-0 font-mono text-zinc-400">{open ? '▾' : '▸'}</span>
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
