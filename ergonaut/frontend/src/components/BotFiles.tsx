import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'

type Source = Awaited<ReturnType<typeof api.botSource>>

const IMAGE = /\.(png|jpe?g|gif|webp|svg)$/i

function size(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

// Read-only browser over the bot folder (admins only): a folder tree and the selected file.
export default function BotFiles({ bot }: { bot: string }) {
  const [files, setFiles] = useState<{ path: string; size: number }[] | null>(null)
  const [truncated, setTruncated] = useState(false)
  const [denied, setDenied] = useState(false)
  const [open, setOpen] = useState<Set<string>>(new Set())
  const [selected, setSelected] = useState<string>('')
  const [source, setSource] = useState<Source | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    setFiles(null)
    setSelected('')
    setSource(null)
    api
      .botTree(bot)
      .then(tree => {
        setFiles(tree.files)
        setTruncated(tree.truncated)
        // Start with the top level open, and bot.yaml selected.
        setOpen(new Set(['']))
        if (tree.files.some(f => f.path === 'bot.yaml')) setSelected('bot.yaml')
      })
      .catch(() => setDenied(true))
  }, [bot])

  useEffect(() => {
    if (!selected) return
    setError('')
    api
      .botSource(bot, selected)
      .then(setSource)
      .catch(e => setError(String(e.message ?? e)))
  }, [bot, selected])

  // Folder -> its direct children (folders end with "/").
  const tree = useMemo(() => {
    const children = new Map<string, Set<string>>()
    for (const f of files ?? []) {
      const parts = f.path.split('/')
      for (let i = 0; i < parts.length; i++) {
        const parent = parts.slice(0, i).join('/')
        const child = parts.slice(0, i + 1).join('/') + (i < parts.length - 1 ? '/' : '')
        if (!children.has(parent)) children.set(parent, new Set())
        children.get(parent)!.add(child)
      }
    }
    return children
  }, [files])
  const sizes = useMemo(() => new Map((files ?? []).map(f => [f.path, f.size])), [files])

  if (denied) return null
  if (!files) return <p className="text-sm text-zinc-500">Loading files…</p>

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
      return (
        <button
          key={entry}
          className={`w-full truncate rounded px-1 py-0.5 text-left font-mono text-xs ${selected === path ? 'bg-indigo-50 text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300' : 'hover:bg-zinc-100 dark:hover:bg-zinc-900'}`}
          style={pad}
          title={`${path} · ${size(sizes.get(path) ?? 0)}`}
          onClick={() => setSelected(path)}
        >
          {name}
        </button>
      )
    })
  }

  return (
    <div className="flex h-[32rem] overflow-hidden rounded-lg border border-zinc-200 dark:border-zinc-800">
      <div className="w-60 shrink-0 overflow-y-auto border-r border-zinc-200 p-1 dark:border-zinc-800">
        {render('', 0)}
        {truncated && <p className="p-2 text-xs text-zinc-500">Only the first 3,000 files are listed.</p>}
      </div>
      <div className="flex min-w-0 flex-1 flex-col">
        {source && source.path === selected ? (
          <>
            <div className="flex items-center gap-3 border-b border-zinc-200 px-3 py-1.5 text-xs dark:border-zinc-800">
              <span className="font-mono">{source.path}</span>
              <span className="text-zinc-500">{size(source.size)}</span>
              {source.url && (
                <a className="ml-auto text-zinc-500 underline" href={source.url} target="_blank" rel="noreferrer">
                  {source.path.endsWith('.jhtml') ? 'Open rendered page' : 'Open'}
                </a>
              )}
            </div>
            <div className="min-h-0 flex-1 overflow-auto">
              {IMAGE.test(source.path) && source.url ? (
                <img src={source.url} alt={source.path} className="max-w-full p-3" />
              ) : source.text != null ? (
                <table className="font-mono text-xs">
                  <tbody>
                    {source.text.split('\n').map((line, i) => (
                      <tr key={i}>
                        <td className="pr-3 pl-3 text-right align-top text-zinc-400 select-none">{i + 1}</td>
                        <td className="pr-3 whitespace-pre-wrap">{line || ' '}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
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
  )
}
