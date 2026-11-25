import { useId, useState } from 'react'
import { CopyButton } from './CopyButton'
import { argsPreview, formatArgs, rawArgs, resultSummary, resultText, toolNameParts, type ArgValue } from './format'
import type { ToolPart } from './types'

function Clamped({ text, long, mono }: { text: string; long: boolean; mono: boolean }) {
  const [more, setMore] = useState(false)
  const id = useId()
  return (
    <div className="ergo-chat__clamp-wrap">
      <div
        id={id}
        className={`ergo-chat__wrap ${mono ? 'ergo-chat__mono' : ''} ${long && !more ? 'ergo-chat__clamp' : ''}`.trim()}
      >
        {text}
      </div>
      {long && (
        <button
          type="button"
          className="ergo-chat__link-btn"
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
      return <span className="ergo-chat__mono">{value.text}</span>
    case 'chips':
      return (
        <ul className="ergo-chat__chips">
          {value.items.map((item, i) => (
            <li key={i} className="ergo-chat__chip ergo-chat__mono">
              {item}
            </li>
          ))}
        </ul>
      )
    case 'json':
      return (
        <pre className="ergo-chat__pre ergo-chat__mono" tabIndex={value.long ? 0 : undefined}>
          {value.text}
        </pre>
      )
  }
}

function ArgsView({ input }: { input: unknown }) {
  const args = formatArgs(input)
  return (
    <div>
      <div className="ergo-chat__tool-row">
        <span className="ergo-chat__tool-label">Arguments</span>
        <CopyButton text={rawArgs(input)} label="arguments" />
      </div>
      {args.kind === 'entries' ? (
        args.entries.length ? (
          <dl className="ergo-chat__args">
            {args.entries.map(({ key, value }) => (
              <div key={key} className="ergo-chat__arg">
                <dt className="ergo-chat__mono">{key}</dt>
                <dd>
                  <ArgValueView value={value} />
                </dd>
              </div>
            ))}
          </dl>
        ) : (
          <div className="ergo-chat__muted">No arguments</div>
        )
      ) : (
        <pre className="ergo-chat__pre ergo-chat__mono">{args.text}</pre>
      )}
    </div>
  )
}

function ResultView({ result, error }: { result: unknown; error: boolean }) {
  const { size, preview } = resultSummary(result)
  const label = error ? 'Error' : 'Result'
  const text = resultText(result)
  return (
    <div className="ergo-chat__tool-row ergo-chat__tool-row--top">
      <details className="ergo-chat__result" open={error}>
        <summary className={error ? 'ergo-chat__danger' : 'ergo-chat__muted'}>
          <span className="ergo-chat__result-label">{label}</span> · {size}
          {preview && <span className="ergo-chat__result-preview ergo-chat__mono">{preview}</span>}
        </summary>
        <pre className="ergo-chat__pre ergo-chat__mono" tabIndex={0} aria-label={`${label} text`}>
          {text}
        </pre>
      </details>
      <CopyButton text={text} label="result" />
    </div>
  )
}

const STATUS = {
  running: { icon: '…', label: 'running' },
  done: { icon: '✓', label: 'done' },
  error: { icon: '✗', label: 'error' },
} as const

/** A tool call: a one-line header (status, name, args preview) that opens to arguments and result. Errors show their result open. */
export function ToolCard({ part }: { part: ToolPart }) {
  const [open, setOpen] = useState(false)
  const bodyId = useId()
  const state = STATUS[part.status] ?? STATUS.running
  const { server, tool } = toolNameParts(part.name)
  const hasResult = part.result !== undefined && part.result !== null
  return (
    <div className={`ergo-chat__tool ergo-chat__tool--${part.status}`} data-status={part.status}>
      <button
        type="button"
        className="ergo-chat__tool-head"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        aria-controls={bodyId}
        title={part.name}
      >
        <span aria-hidden className={`ergo-chat__tool-icon ergo-chat__tool-icon--${part.status}`}>
          {state.icon}
        </span>
        <span className="ergo-chat__sr">{state.label}:</span>
        <span className="ergo-chat__tool-name ergo-chat__mono">
          {server && <span className="ergo-chat__muted">{server} · </span>}
          {tool}
        </span>
        <span className="ergo-chat__tool-preview ergo-chat__mono">{argsPreview(part.input)}</span>
        <span className="ergo-chat__tool-state">{part.status === 'done' ? '' : state.label}</span>
        <span aria-hidden className="ergo-chat__muted ergo-chat__mono">
          {open ? '▾' : '▸'}
        </span>
      </button>
      {open && (
        <div id={bodyId} className="ergo-chat__tool-body">
          {part.input !== undefined && <ArgsView input={part.input} />}
          {hasResult && <ResultView result={part.result} error={part.status === 'error'} />}
          {part.status === 'running' && !hasResult && <div className="ergo-chat__muted">Running…</div>}
          <div className="ergo-chat__tool-id ergo-chat__mono">{part.id}</div>
        </div>
      )}
    </div>
  )
}
