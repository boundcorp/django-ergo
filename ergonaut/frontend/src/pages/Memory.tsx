import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import type { KB, KBArticle } from '../api'
import { api } from '../api'

// Browse the knowledge bases attached to a bot: articles on the left, the
// selected article on the right.
export function Memory() {
  const { name = '' } = useParams()
  const [kbs, setKbs] = useState<KB[] | null>(null)
  const [selected, setSelected] = useState<{ kb: string; path: string } | null>(null)
  const [article, setArticle] = useState<KBArticle | null>(null)
  const [filter, setFilter] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    setKbs(null)
    setSelected(null)
    api
      .kbs(name)
      .then(found => {
        setKbs(found)
        const first = found.find(kb => kb.articles.length)
        if (first) setSelected({ kb: first.id, path: first.articles[0].path })
      })
      .catch(e => setError(String(e.message ?? e)))
  }, [name])

  useEffect(() => {
    if (!selected) return setArticle(null)
    api
      .kbArticle(name, selected.kb, selected.path)
      .then(setArticle)
      .catch(e => setError(String(e.message ?? e)))
  }, [name, selected])

  if (!kbs) return <div className="p-6 text-zinc-500">{error || 'Loading…'}</div>

  const needle = filter.toLowerCase()
  return (
    <div className="flex h-full">
      <div className="flex w-72 shrink-0 flex-col border-r border-zinc-200 dark:border-zinc-800">
        <div className="border-b border-zinc-200 p-3 dark:border-zinc-800">
          <div className="text-sm font-semibold">
            <Link to={`/bots/${name}`} className="hover:underline">
              {name}
            </Link>{' '}
            memory
          </div>
          <input
            value={filter}
            onChange={e => setFilter(e.target.value)}
            placeholder="Filter articles"
            className="mt-2 w-full rounded-md border border-zinc-300 bg-transparent px-2 py-1 text-sm dark:border-zinc-700"
          />
        </div>
        <div className="flex-1 overflow-y-auto p-2">
          {!kbs.length && (
            <p className="p-2 text-sm text-zinc-500">
              This bot has no knowledge base. Add Markdown files to a <code>kb/</code> folder in the bot folder.
            </p>
          )}
          {kbs.map(kb => (
            <section key={kb.id} className="mb-3">
              <div className="px-2 text-xs font-semibold tracking-wide text-zinc-500 uppercase" title={kb.location}>
                {kb.name} · {kb.kind}
              </div>
              {kb.articles
                .filter(a => !needle || a.title.toLowerCase().includes(needle) || a.path.toLowerCase().includes(needle))
                .map(a => {
                  const active = selected?.kb === kb.id && selected.path === a.path
                  return (
                    <button
                      key={a.path}
                      onClick={() => setSelected({ kb: kb.id, path: a.path })}
                      className={`block w-full truncate rounded-md px-2 py-1 text-left text-sm ${
                        active ? 'bg-zinc-200 font-medium dark:bg-zinc-800' : 'hover:bg-zinc-100 dark:hover:bg-zinc-900'
                      }`}
                    >
                      {a.root && (
                        <span className="mr-1 text-amber-500" title="Root article: always in context">
                          ★
                        </span>
                      )}
                      {a.title}
                      {kb.kind === 'folder' && (
                        <span className="ml-2 font-mono text-[11px] text-zinc-400">{a.path}</span>
                      )}
                    </button>
                  )
                })}
              {!kb.articles.length && <p className="px-2 text-sm text-zinc-500">No articles yet.</p>}
            </section>
          ))}
        </div>
      </div>
      <div className="min-w-0 flex-1 overflow-y-auto px-8 py-6">
        {error && <p className="text-sm text-red-600">{error}</p>}
        {article ? (
          <>
            <div className="mb-4 font-mono text-xs text-zinc-500">{article.path}</div>
            <div className="max-w-3xl text-sm leading-6 whitespace-pre-wrap">{article.body}</div>
          </>
        ) : (
          !error && <p className="text-zinc-500">Pick an article.</p>
        )}
      </div>
    </div>
  )
}
