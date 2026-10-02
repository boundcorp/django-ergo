import { useEffect, useState } from 'react'
import { api } from '../api'

type Page = Awaited<ReturnType<typeof api.tableRows>>

function show(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

// Rows of one bot table: sortable columns, a text search, and paging.
export default function TableBrowser({ bot, table, onClose }: { bot: string; table: string; onClose: () => void }) {
  const [page, setPage] = useState(1)
  const [order, setOrder] = useState('')
  const [search, setSearch] = useState('')
  const [query, setQuery] = useState('')
  const [data, setData] = useState<Page | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let current = true
    setError('')
    api
      .tableRows(bot, table, { page, order, q: query })
      .then(d => current && setData(d))
      .catch(e => current && setError(String(e.message ?? e)))
    return () => {
      current = false
    }
  }, [bot, table, page, order, query])

  const pages = data ? Math.max(1, Math.ceil(data.count / data.page_size)) : 1
  const sortBy = (name: string) => {
    setOrder(o => (o === name ? `-${name}` : o === `-${name}` ? '' : name))
    setPage(1)
  }

  return (
    <div className="mt-3 rounded-lg border border-zinc-200 dark:border-zinc-800">
      <div className="flex flex-wrap items-center gap-2 border-b border-zinc-200 px-3 py-2 text-sm dark:border-zinc-800">
        <span className="font-mono font-medium">{table}</span>
        {data && (
          <span className="text-xs text-zinc-500">
            {data.count.toLocaleString()} rows{data.description ? ` · ${data.description}` : ''}
          </span>
        )}
        <form
          className="ml-auto"
          onSubmit={e => {
            e.preventDefault()
            setQuery(search)
            setPage(1)
          }}
        >
          <input
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Search text"
            className="rounded-md border border-zinc-300 bg-transparent px-2 py-0.5 text-xs dark:border-zinc-700"
          />
        </form>
        <button className="text-xs text-zinc-500 hover:text-zinc-900 dark:hover:text-zinc-100" onClick={onClose}>
          Close ✕
        </button>
      </div>
      {error && <p className="p-3 text-sm text-red-600">{error}</p>}
      {data && (
        <div className="max-h-[32rem] overflow-auto">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-zinc-50 dark:bg-zinc-900">
              <tr>
                {data.fields.map(f => (
                  <th
                    key={f.name}
                    title={f.type}
                    className="cursor-pointer px-2 py-1.5 text-left font-medium whitespace-nowrap text-zinc-500 hover:text-zinc-900 dark:hover:text-zinc-100"
                    onClick={() => sortBy(f.name)}
                  >
                    {f.name}
                    {order === f.name ? ' ▲' : order === `-${f.name}` ? ' ▼' : ''}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">
              {data.rows.map((row, i) => (
                <tr key={String(row.id ?? i)}>
                  {data.fields.map(f => (
                    <td
                      key={f.name}
                      className="max-w-xs truncate px-2 py-1 font-mono whitespace-nowrap"
                      title={show(row[f.name])}
                    >
                      {show(row[f.name])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          {!data.rows.length && <p className="p-3 text-sm text-zinc-500">No rows.</p>}
        </div>
      )}
      {data && pages > 1 && (
        <div className="flex items-center gap-2 border-t border-zinc-200 px-3 py-1.5 text-xs dark:border-zinc-800">
          <button disabled={page <= 1} className="disabled:opacity-40" onClick={() => setPage(p => p - 1)}>
            ← Prev
          </button>
          <span className="text-zinc-500">
            Page {page} of {pages}
          </span>
          <button disabled={page >= pages} className="disabled:opacity-40" onClick={() => setPage(p => p + 1)}>
            Next →
          </button>
        </div>
      )}
    </div>
  )
}
