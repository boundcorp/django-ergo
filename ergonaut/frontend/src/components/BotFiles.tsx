import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import CodeView from './CodeView'

type Source = Awaited<ReturnType<typeof api.botSource>>
type Proposal = Awaited<ReturnType<typeof api.botProposals>>['proposals'][number]

const IMAGE = /\.(png|jpe?g|gif|webp|svg)$/i
const STATUS: Record<string, { label: string; className: string }> = {
  A: { label: 'added', className: 'text-emerald-600' },
  M: { label: 'changed', className: 'text-amber-600' },
  D: { label: 'deleted', className: 'text-red-600 line-through' },
}

function size(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function DiffView({ diff }: { diff: string }) {
  return (
    <pre className="p-3 font-mono text-xs">
      {diff.split('\n').map((line, i) => (
        <div
          key={i}
          className={
            line.startsWith('+') && !line.startsWith('+++')
              ? 'bg-emerald-500/15'
              : line.startsWith('-') && !line.startsWith('---')
                ? 'bg-red-500/15'
                : line.startsWith('@@')
                  ? 'text-indigo-500'
                  : ''
          }
        >
          {line || ' '}
        </div>
      ))}
    </pre>
  )
}

// Read-only browser over the bot folder (admins only): live, or as a proposal (the unpublished
// draft or an open pull request) would leave it, with what changed and the actions to take.
export default function BotFiles({ bot }: { bot: string }) {
  const [version, setVersion] = useState('live')
  const [proposals, setProposals] = useState<Proposal[]>([])
  const [files, setFiles] = useState<{ path: string; size: number; status: string }[] | null>(null)
  const [truncated, setTruncated] = useState(false)
  const [denied, setDenied] = useState(false)
  const [changedOnly, setChangedOnly] = useState(false)
  const [open, setOpen] = useState<Set<string>>(new Set(['']))
  const [selected, setSelected] = useState<string>('')
  const [source, setSource] = useState<Source | null>(null)
  const [showDiff, setShowDiff] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')

  const loadProposals = useCallback(
    () =>
      api
        .botProposals(bot)
        .then(p => setProposals(p.proposals))
        .catch(() => setProposals([])),
    [bot],
  )
  useEffect(() => {
    setVersion('live')
    loadProposals()
  }, [bot, loadProposals])

  useEffect(() => {
    let current = true
    setFiles(null)
    setSource(null)
    setError('')
    api
      .botTree(bot, version)
      .then(tree => {
        if (!current) return
        setFiles(tree.files)
        setTruncated(tree.truncated)
        const changed = tree.files.filter(f => f.status)
        setChangedOnly(version !== 'live' && changed.length > 0)
        const first = version !== 'live' && changed.length ? changed[0].path : 'bot.yaml'
        setSelected(tree.files.some(f => f.path === first) ? first : '')
        // Open the folders holding changes.
        const folders = new Set([''])
        for (const f of changed) {
          const parts = f.path.split('/')
          for (let i = 1; i < parts.length; i++) folders.add(parts.slice(0, i).join('/'))
        }
        setOpen(folders)
      })
      .catch(e => current && (version === 'live' ? setDenied(true) : setError(String(e.message ?? e))))
    return () => {
      current = false
    }
  }, [bot, version])

  useEffect(() => {
    if (!selected) return
    // A slower answer for an earlier file or version must not replace this one.
    let current = true
    setError('')
    api
      .botSource(bot, selected, version)
      .then(s => {
        if (!current) return
        setSource(s)
        setShowDiff(!!s.diff)
      })
      .catch(e => current && setError(String(e.message ?? e)))
    return () => {
      current = false
    }
  }, [bot, selected, version])

  const visible = useMemo(() => (files ?? []).filter(f => !changedOnly || f.status), [files, changedOnly])
  // Folder -> its direct children (folders end with "/").
  const tree = useMemo(() => {
    const children = new Map<string, Set<string>>()
    for (const f of visible) {
      const parts = f.path.split('/')
      for (let i = 0; i < parts.length; i++) {
        const parent = parts.slice(0, i).join('/')
        const child = parts.slice(0, i + 1).join('/') + (i < parts.length - 1 ? '/' : '')
        if (!children.has(parent)) children.set(parent, new Set())
        children.get(parent)!.add(child)
      }
    }
    return children
  }, [visible])
  const byPath = useMemo(() => new Map((files ?? []).map(f => [f.path, f])), [files])

  if (denied) return null
  const proposal = proposals.find(p => p.version === version)

  async function act(action: () => Promise<{ result: string }>, confirmText: string) {
    if (!confirm(confirmText)) return
    setBusy(true)
    setNotice('')
    try {
      const { result } = await action()
      setNotice(result)
      setVersion('live')
      await loadProposals()
    } catch (e) {
      setError(String((e as Error).message ?? e))
    } finally {
      setBusy(false)
    }
  }

  function render(folder: string, depth: number): React.ReactNode {
    const entries = [...(tree.get(folder) ?? [])].sort((a, b) => {
      const da = a.endsWith('/') ? 0 : 1
      const db = b.endsWith('/') ? 0 : 1
      return da - db || a.localeCompare(b)
    })
    return entries.map(entry => {
      const isDir = entry.endsWith('/')
      const path = isDir ? entry.slice(0, -1) : entry
      const name = path.split('/').pop()
      const pad = { paddingLeft: `${depth * 14 + 6}px` }
      if (isDir) {
        const expanded = open.has(path)
        return (
          <div key={entry}>
            <button
              className="w-full truncate rounded px-1 py-0.5 text-left text-sm hover:bg-zinc-100 dark:hover:bg-zinc-900"
              style={pad}
              onClick={() =>
                setOpen(s => {
                  const next = new Set(s)
                  if (expanded) next.delete(path)
                  else next.add(path)
                  return next
                })
              }
            >
              {expanded ? '▾' : '▸'} {name}/
            </button>
            {expanded && render(path, depth + 1)}
          </div>
        )
      }
      const status = STATUS[byPath.get(path)?.status ?? '']
      return (
        <button
          key={entry}
          className={`flex w-full items-center gap-1 truncate rounded px-1 py-0.5 text-left font-mono text-xs ${selected === path ? 'bg-indigo-50 text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300' : 'hover:bg-zinc-100 dark:hover:bg-zinc-900'}`}
          style={pad}
          title={status ? `${path} (${status.label})` : path}
          onClick={() => setSelected(path)}
        >
          <span className={`truncate ${status?.className ?? ''}`}>{name}</span>
          {status && (
            <span className={`ml-auto shrink-0 ${status.className.split(' ')[0]}`}>
              {status.label[0].toUpperCase()}
            </span>
          )}
        </button>
      )
    })
  }

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-2 text-sm">
        <select
          className="rounded-md border border-zinc-300 bg-transparent px-2 py-1 dark:border-zinc-700"
          value={version}
          onChange={e => setVersion(e.target.value)}
        >
          <option value="live">Live (what's running)</option>
          {proposals.map(p => (
            <option key={p.version} value={p.version}>
              {p.number ? `PR #${p.number}: ${p.title}` : p.title} ({Object.keys(p.changed).length} files)
            </option>
          ))}
        </select>
        {!proposals.length && <span className="text-xs text-zinc-500">No proposed changes to this bot.</span>}
        {version !== 'live' && (
          <label className="flex items-center gap-1 text-xs text-zinc-500">
            <input type="checkbox" checked={changedOnly} onChange={e => setChangedOnly(e.target.checked)} />
            changed files only
          </label>
        )}
        {proposal?.number && (
          <>
            <a className="text-xs text-zinc-500 underline" href={proposal.url} target="_blank" rel="noreferrer">
              on GitHub
            </a>
            <button
              disabled={busy}
              className="ml-auto rounded-control bg-success px-3 py-1 text-xs text-canvas disabled:opacity-50"
              onClick={() =>
                act(() => api.mergeChange(bot, proposal.number!), `Merge PR #${proposal.number} and make it live?`)
              }
            >
              Merge
            </button>
            <button
              disabled={busy}
              className="rounded-md border border-zinc-300 px-3 py-1 text-xs disabled:opacity-50 dark:border-zinc-700"
              onClick={() => act(() => api.closeChange(bot, proposal.number!), `Close PR #${proposal.number}?`)}
            >
              Close
            </button>
          </>
        )}
        {proposal && !proposal.number && (
          <button
            disabled={busy}
            className="ml-auto rounded-md border border-zinc-300 px-3 py-1 text-xs disabled:opacity-50 dark:border-zinc-700"
            onClick={() => act(() => api.discardDraft(bot), 'Discard the unpublished changes?')}
          >
            Discard draft
          </button>
        )}
      </div>
      {notice && <p className="mb-2 text-xs text-emerald-600">{notice}</p>}
      <div className="flex h-[32rem] overflow-hidden rounded-lg border border-zinc-200 dark:border-zinc-800">
        <div className="w-60 shrink-0 overflow-y-auto border-r border-zinc-200 p-1 dark:border-zinc-800">
          {files ? render('', 0) : <p className="p-2 text-sm text-zinc-500">Loading files…</p>}
          {truncated && <p className="p-2 text-xs text-zinc-500">Only the first 3,000 files are listed.</p>}
        </div>
        <div className="flex min-w-0 flex-1 flex-col">
          {source && source.path === selected ? (
            <>
              <div className="flex items-center gap-3 border-b border-zinc-200 px-3 py-1.5 text-xs dark:border-zinc-800">
                <span className="font-mono">{source.path}</span>
                {!source.deleted && <span className="text-zinc-500">{size(source.size)}</span>}
                {source.diff && (
                  <span className="flex gap-1">
                    <button className={showDiff ? 'font-semibold' : 'text-zinc-500'} onClick={() => setShowDiff(true)}>
                      Changes
                    </button>
                    ·
                    <button
                      className={!showDiff ? 'font-semibold' : 'text-zinc-500'}
                      onClick={() => setShowDiff(false)}
                    >
                      File
                    </button>
                  </span>
                )}
                {source.url && (
                  <a className="ml-auto text-zinc-500 underline" href={source.url} target="_blank" rel="noreferrer">
                    {source.path.endsWith('.jhtml') ? 'Open rendered page' : 'Open'}
                  </a>
                )}
              </div>
              <div className="min-h-0 flex-1 overflow-auto">
                {source.diff && showDiff ? (
                  <DiffView diff={source.diff} />
                ) : source.deleted ? (
                  <p className="p-3 text-sm text-red-600">This proposal deletes the file.</p>
                ) : IMAGE.test(source.path) && source.url ? (
                  <img src={source.url} alt={source.path} className="max-w-full p-3" />
                ) : source.text != null ? (
                  <CodeView path={source.path} text={source.text} />
                ) : (
                  <p className="p-3 text-sm text-zinc-500">
                    {source.size > 500_000 ? 'Too large to show here.' : 'Not a text file.'}
                  </p>
                )}
              </div>
            </>
          ) : (
            <p className="p-3 text-sm text-zinc-500">{error || (selected ? 'Loading…' : 'Pick a file.')}</p>
          )}
        </div>
      </div>
    </div>
  )
}
