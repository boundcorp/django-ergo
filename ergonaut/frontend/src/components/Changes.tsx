import { useCallback, useEffect, useState } from 'react'
import type { Changes as ChangesData, PullRequest } from '../api'
import { api } from '../api'

function Diff({ text }: { text: string }) {
  return (
    <pre className="max-h-[32rem] overflow-auto rounded-lg bg-zinc-50 p-3 text-xs leading-5 dark:bg-zinc-900">
      {text.split('\n').map((line, i) => (
        <div
          key={i}
          className={
            line.startsWith('+') && !line.startsWith('+++')
              ? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
              : line.startsWith('-') && !line.startsWith('---')
                ? 'bg-red-500/10 text-red-700 dark:text-red-300'
                : line.startsWith('@@') || line.startsWith('diff ')
                  ? 'text-indigo-600 dark:text-indigo-400'
                  : ''
          }
        >
          {line || ' '}
        </div>
      ))}
    </pre>
  )
}

function Proposal({ bot, pr, onDone }: { bot: string; pr: PullRequest; onDone: (msg: string) => void }) {
  const [diff, setDiff] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function act(kind: 'merge' | 'close') {
    const verb = kind === 'merge' ? 'Merge' : 'Close'
    if (!confirm(`${verb} #${pr.number} “${pr.title}”?`)) return
    setBusy(true)
    try {
      const { result } =
        kind === 'merge' ? await api.mergeChange(bot, pr.number) : await api.closeChange(bot, pr.number)
      onDone(result + (kind === 'merge' ? ' The bots reload in a few seconds.' : ''))
    } catch (e) {
      onDone(`${verb} failed: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="rounded-lg border border-zinc-200 p-3 dark:border-zinc-800">
      <div className="flex flex-wrap items-center gap-2">
        <a
          href={pr.url}
          target="_blank"
          rel="noreferrer"
          className="font-medium text-indigo-600 hover:underline dark:text-indigo-400"
        >
          #{pr.number} {pr.title}
        </a>
        <span className="text-xs text-zinc-500">
          {pr.author && `by ${pr.author} · `}
          {pr.changed_files} files · <span className="text-emerald-600">+{pr.additions}</span>{' '}
          <span className="text-red-600">−{pr.deletions}</span>
        </span>
        <div className="ml-auto flex gap-2">
          <button
            className="rounded-md border border-zinc-300 px-2 py-0.5 text-xs dark:border-zinc-700"
            onClick={() => (diff === null ? api.changeDiff(bot, pr.number).then(d => setDiff(d.diff)) : setDiff(null))}
          >
            {diff === null ? 'Show diff' : 'Hide diff'}
          </button>
          <button
            disabled={busy}
            className="rounded-md bg-emerald-600 px-2 py-0.5 text-xs text-white disabled:opacity-50"
            onClick={() => act('merge')}
          >
            Merge
          </button>
          <button
            disabled={busy}
            className="rounded-md border border-zinc-300 px-2 py-0.5 text-xs disabled:opacity-50 dark:border-zinc-700"
            onClick={() => act('close')}
          >
            Close
          </button>
        </div>
      </div>
      {pr.body && <p className="mt-1 text-sm whitespace-pre-wrap text-zinc-600 dark:text-zinc-400">{pr.body}</p>}
      {diff !== null && (
        <div className="mt-2">
          <Diff text={diff} />
        </div>
      )}
    </div>
  )
}

// Proposed changes to the bot repo: open pull requests and the unpublished draft.
export default function Changes({ bot }: { bot: string }) {
  const [data, setData] = useState<ChangesData | null>(null)
  const [note, setNote] = useState('')
  const load = useCallback(
    () =>
      api
        .changes(bot)
        .then(setData)
        .catch(e => setNote(String(e.message ?? e))),
    [bot],
  )
  useEffect(() => {
    load()
  }, [load])

  if (!data) return <p className="text-sm text-zinc-500">{note || 'Loading…'}</p>
  const done = (msg: string) => {
    setNote(msg)
    load()
  }
  return (
    <div className="flex flex-col gap-3">
      {note && <p className="text-sm text-zinc-600 dark:text-zinc-400">{note}</p>}
      {data.error && <p className="text-sm text-red-600">{data.error}</p>}
      {data.pull_requests.map(pr => (
        <Proposal key={pr.number} bot={bot} pr={pr} onDone={done} />
      ))}
      {!data.pull_requests.length && <p className="text-sm text-zinc-500">No open proposals.</p>}
      {data.draft_diff && (
        <div className="rounded-lg border border-dashed border-zinc-300 p-3 dark:border-zinc-700">
          <div className="mb-2 flex items-center">
            <span className="text-sm font-medium">Unpublished draft</span>
            <span className="ml-2 text-xs text-zinc-500">{data.managed_by} hasn't proposed this yet</span>
            <button
              className="ml-auto rounded-md border border-zinc-300 px-2 py-0.5 text-xs dark:border-zinc-700"
              onClick={async () => {
                if (!confirm('Throw away the unpublished draft?')) return
                done((await api.discardDraft(bot)).result)
              }}
            >
              Discard
            </button>
          </div>
          <Diff text={data.draft_diff} />
        </div>
      )}
      <p className="text-xs text-zinc-500">
        {data.repo} ·{' '}
        {data.mode === 'propose_pr' ? 'changes are proposed as pull requests' : 'changes are pushed to main'}
      </p>
    </div>
  )
}
