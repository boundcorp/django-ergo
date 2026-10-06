import { useId, useRef, useState } from 'react'
import type { Block } from '../api'
import {
  argsPreview,
  formatArgs,
  rawArgs,
  resultSummary,
  resultText,
  toolNameParts,
  type ArgValue,
} from '../toolFormat'

type ToolUse = Extract<Block, { type: 'tool_use' }>
type ToolResult = Extract<Block, { type: 'tool_result' }>

function seconds(ms: number): string {
  if (ms < 1000) return `${Math.max(0, Math.round(ms))}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)}s`
  return `${Math.floor(ms / 60_000)}m${Math.round((ms % 60_000) / 1000)}s`
}

/** Copies text; falls back to a hidden textarea where the clipboard API needs https. */
async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    const area = document.createElement('textarea')
    area.value = text
    area.setAttribute('readonly', '')
    area.style.position = 'fixed'
    area.style.opacity = '0'
    document.body.appendChild(area)
    area.select()
    document.execCommand('copy')
    area.remove()
  }
}

const SMALL_BUTTON =
  'min-h-10 shrink-0 rounded-control border border-stroke px-2 text-xs text-muted hover:border-accent hover:text-ink sm:min-h-6 sm:px-1.5'

function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false)
  const timer = useRef<number | undefined>(undefined)
  return (
    <>
      <button
        type="button"
        className={SMALL_BUTTON}
        aria-label={`Copy ${label}`}
        onClick={async () => {
          await copyText(text)
          setCopied(true)
          window.clearTimeout(timer.current)
          timer.current = window.setTimeout(() => setCopied(false), 1500)
        }}
      >
        {copied ? 'Copied' : 'Copy'}
      </button>
      <span className="sr-only" role="status">
        {copied ? `Copied ${label}` : ''}
      </span>
    </>
  )
}

/** A string clamped to a few lines, with "show more" for the rest. */
function Clamped({ text, long, mono }: { text: string; long: boolean; mono: boolean }) {
  const [more, setMore] = useState(false)
  const id = useId()
  return (
    <div className="min-w-0">
      <div
        id={id}
        className={`whitespace-pre-wrap [overflow-wrap:anywhere] ${mono ? 'font-mono text-xs' : ''} ${long && !more ? 'line-clamp-3' : ''}`}
      >
        {text}
      </div>
      {long && (
        <button
          type="button"
          className="min-h-10 text-xs text-accent hover:underline sm:min-h-6"
          aria-expanded={more}
          aria-controls={id}
          onClick={() => setMore(!more)}
        >
          {more ? 'Show less' : 'Show more'}
        </button>
      )}
    </div>
  )
}

function ArgValueView({ value }: { value: ArgValue }) {
  switch (value.kind) {
    case 'text':
      return <Clamped text={value.text} long={value.long} mono={value.mono} />
    case 'scalar':
      return <span className="font-mono text-xs">{value.text}</span>
    case 'chips':
      return (
        <ul className="flex flex-wrap gap-1">
          {value.items.map((item, i) => (
            <li
              key={i}
              className="rounded-control border border-stroke bg-raised px-1.5 font-mono text-xs [overflow-wrap:anywhere]"
            >
              {item}
            </li>
          ))}
        </ul>
      )
    case 'json':
      return (
        <pre
          className={`overflow-auto rounded-control bg-raised px-2 py-1 font-mono text-xs whitespace-pre-wrap [overflow-wrap:anywhere] ${value.long ? 'max-h-40' : ''}`}
          tabIndex={value.long ? 0 : undefined}
        >
          {value.text}
        </pre>
      )
  }
}

/** The call's arguments as a key/value list, or as raw JSON on request. */
function ArgsView({ input }: { input: unknown }) {
  const [raw, setRaw] = useState(false)
  const args = formatArgs(input)
  const full = rawArgs(input)
  return (
    <div>
      <div className="mb-1 flex items-center gap-2">
        <div className="text-xs font-medium text-muted">Arguments</div>
        <div className="ml-auto flex items-center gap-1">
          {args.kind === 'entries' && (
            <button type="button" className={SMALL_BUTTON} aria-pressed={raw} onClick={() => setRaw(!raw)}>
              Raw JSON
            </button>
          )}
          <CopyButton text={full} label="arguments" />
        </div>
      </div>
      {args.kind === 'entries' && !raw ? (
        args.entries.length ? (
          <dl className="grid grid-cols-[minmax(0,max-content)_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs sm:text-sm">
            {args.entries.map(({ key, value }) => (
              <div key={key} className="contents">
                <dt className="max-w-[40vw] font-mono text-xs text-muted [overflow-wrap:anywhere]">{key}</dt>
                <dd className="m-0 min-w-0">
                  <ArgValueView value={value} />
                </dd>
              </div>
            ))}
          </dl>
        ) : (
          <div className="text-xs text-muted">No arguments</div>
        )
      ) : (
        <pre className="max-h-80 overflow-auto font-mono text-xs whitespace-pre-wrap [overflow-wrap:anywhere]">
          {full}
        </pre>
      )}
    </div>
  )
}

/** The result, folded behind one line (size and first line) until opened. Errors start open. */
function ResultView({ result }: { result: ToolResult }) {
  const [opened, setOpened] = useState<boolean | null>(null)
  const open = opened ?? !!result.is_error
  const { size, preview } = resultSummary(result.content)
  const label = result.is_error ? 'Error' : 'Result'
  return (
    <div className="flex items-start gap-2">
      <details className="min-w-0 flex-1" open={open} onToggle={event => setOpened(event.currentTarget.open)}>
        <summary
          className={`flex min-h-10 cursor-pointer list-none items-center gap-2 rounded-control text-xs sm:min-h-6 ${result.is_error ? 'text-danger' : 'text-muted'}`}
        >
          <span aria-hidden className="w-3 shrink-0 text-center font-mono">
            {open ? '▾' : '▸'}
          </span>
          <span className="shrink-0 font-medium">{label}</span>
          <span className="shrink-0">· {size}</span>
          {!open && preview && <span className="min-w-0 truncate font-mono">{preview}</span>}
        </summary>
        <pre
          className="mt-1 max-h-80 overflow-auto rounded-control bg-raised px-2 py-1 font-mono text-xs whitespace-pre-wrap [overflow-wrap:anywhere]"
          tabIndex={0}
          aria-label={`${label} text`}
        >
          {resultText(result.content)}
        </pre>
      </details>
      <CopyButton text={resultText(result.content)} label="result" />
    </div>
  )
}

/** A tool call: a one-line header (status, name, args preview, time) that opens to the full
 *  arguments and a folded result. Every call starts folded; ``compact`` (calls inside a
 *  "+N more" fold) only sets the smaller size. Open state is the reader's choice and sticks
 *  across re-renders while a chat streams. */
export function ToolCard({
  use,
  result,
  pending,
  duration,
  compact = false,
}: {
  use: ToolUse
  result?: ToolResult
  pending?: boolean
  duration?: number | null // ms from the call to its result
  compact?: boolean
}) {
  const [open, setOpen] = useState(false)
  const bodyId = useId()
  const [icon, state, tone] = pending
    ? ['⏸', 'needs approval', 'border-warning/50 bg-amber-tint']
    : !result
      ? ['…', 'running', 'border-stroke bg-surface']
      : result.is_error
        ? ['✗', 'error', 'border-danger/50 bg-red-tint']
        : ['✓', 'ok', 'border-stroke bg-surface']
  const { server, tool } = toolNameParts(use.name)
  return (
    <div className={`min-w-0 rounded-control border ${compact ? 'text-xs' : 'text-sm'} ${tone}`}>
      <button
        type="button"
        className={`flex min-h-10 w-full items-center gap-2 px-2.5 text-left sm:min-h-0 ${compact ? 'sm:py-1' : 'sm:py-1.5'}`}
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        aria-controls={bodyId}
        title={use.name}
      >
        <span
          aria-hidden
          className={`w-3 shrink-0 text-center ${result?.is_error ? 'text-danger' : pending ? 'text-warning' : result ? 'text-success' : 'text-accent motion-safe:animate-pulse'}`}
        >
          {icon}
        </span>
        <span className="sr-only">{state}:</span>
        <span className="max-w-[55%] shrink-0 truncate font-mono font-medium">
          {server && <span className="font-normal text-muted">{server} · </span>}
          {tool}
        </span>
        <span className="min-w-0 truncate font-mono text-xs text-muted">{argsPreview(use.input)}</span>
        <span className="ml-auto shrink-0 text-xs text-muted">
          {duration != null && result ? seconds(duration) : state !== 'ok' ? state : ''}
        </span>
        <span aria-hidden className="shrink-0 font-mono text-muted">
          {open ? '▾' : '▸'}
        </span>
      </button>
      {open && (
        <div id={bodyId} className="space-y-2 border-t border-inherit px-3 py-2">
          <ArgsView input={use.input} />
          {result && <ResultView result={result} />}
          {!result && !pending && <div className="text-xs text-muted motion-safe:animate-pulse">Running…</div>}
          <div className="font-mono text-[11px] text-muted [overflow-wrap:anywhere]">{use.id}</div>
        </div>
      )}
    </div>
  )
}
