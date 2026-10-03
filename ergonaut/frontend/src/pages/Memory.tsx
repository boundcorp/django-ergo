import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import type { KB, KBArticle } from '../api'
import { api } from '../api'
import Markdown from '../components/Markdown'

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
    <div className="page-content flex h-full flex-col overflow-y-auto">
      <h1 className="page-title mb-1">{name} memory</h1>
      <div className="surface-card mt-6 flex min-h-0 flex-1 flex-col overflow-hidden sm:flex-row">
        <div className="flex w-full shrink-0 flex-col border-b border-stroke bg-raised sm:w-72 sm:border-r sm:border-b-0">
          <div className="border-b border-stroke p-4">
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
              className="mt-3 w-full rounded-control border border-stroke bg-surface px-3 py-2 text-sm"
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
                  .filter(
                    a => !needle || a.title.toLowerCase().includes(needle) || a.path.toLowerCase().includes(needle),
                  )
                  .map(a => {
                    const active = selected?.kb === kb.id && selected.path === a.path
                    return (
                      <button
                        key={a.path}
                        onClick={() => setSelected({ kb: kb.id, path: a.path })}
                        className={`block w-full truncate rounded-md px-2 py-1 text-left text-sm ${
                          active ? 'bg-indigo-tint font-medium text-accent-soft' : 'hover:bg-surface'
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
        <div className="min-w-0 flex-1 overflow-y-auto px-6 py-6">
          {error && <p className="text-sm text-red-600">{error}</p>}
          {article ? (
            <>
              <div className="mb-4 font-mono text-xs text-teal">{article.path}</div>
              <Markdown text={article.body} className="max-w-3xl text-sm leading-6" />
            </>
          ) : (
            !error && <p className="text-zinc-500">Pick an article.</p>
          )}
        </div>
      </div>
    </div>
  )
}
