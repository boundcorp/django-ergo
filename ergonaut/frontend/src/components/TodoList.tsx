import { useState } from 'react'
import type { Todo } from '../api'

const MARK: Record<Todo['status'], string> = {
  pending: '○',
  in_progress: '◐',
  completed: '✓',
}

/** "2 of 5 done", for the card header and the phone session bar. */
export function todoProgress(todos: Todo[]): string {
  const done = todos.filter(t => t.status === 'completed').length
  return `${done} of ${todos.length} done`
}

// The chat's todo list (django_ergo.bots.todos): the model writes it and checks items off.
// Open while anything is left; a finished list folds to its header.
export function TodoList({ todos }: { todos: Todo[] }) {
  const finished = todos.every(t => t.status === 'completed')
  const [toggled, setToggled] = useState<boolean | null>(null)
  if (!todos.length) return null
  const open = toggled ?? !finished
  return (
    <div className="mx-4 mb-2 flex flex-col gap-1 rounded-card border border-stroke bg-surface px-4 py-3 text-xs sm:mx-6">
      <button
        type="button"
        aria-expanded={open}
        className="flex items-center gap-2 self-start text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300"
        onClick={() => setToggled(!open)}
      >
        <span>{open ? '▾' : '▸'}</span>
        <span className="font-medium text-ink">Todo</span>
        <span>{todoProgress(todos)}</span>
      </button>
      {open && (
        <ol className="flex flex-col gap-0.5">
          {todos.map((t, i) => (
            <li key={i} className="flex items-baseline gap-2" data-status={t.status}>
              <span
                className={
                  t.status === 'completed'
                    ? 'text-emerald-600'
                    : t.status === 'in_progress'
                      ? 'text-indigo-500'
                      : 'text-zinc-400'
                }
              >
                {MARK[t.status] ?? MARK.pending}
              </span>
              <span
                className={
                  t.status === 'completed'
                    ? 'text-zinc-400 line-through'
                    : t.status === 'in_progress'
                      ? 'font-medium text-ink'
                      : 'text-zinc-600 dark:text-zinc-400'
                }
              >
                {t.content}
              </span>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}
