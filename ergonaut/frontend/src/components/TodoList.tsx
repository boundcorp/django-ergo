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

const STORAGE_KEY = 'ergonaut.todos'

export function todosShown(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) === 'open'
  } catch {
    return false
  }
}

export function saveTodosShown(open: boolean) {
  try {
    localStorage.setItem(STORAGE_KEY, open ? 'open' : 'closed')
  } catch {
    // Storage may be unavailable in private browsing.
  }
}

/** The floating "Todo X/Y" button beside Illustrate: shows or hides the list above the composer. */
export function TodoButton({
  todos,
  open,
  controls,
  onToggle,
}: {
  todos: Todo[]
  open: boolean
  controls: string
  onToggle: () => void
}) {
  if (!todos.length) return null
  const done = todos.filter(t => t.status === 'completed').length
  return (
    <button
      type="button"
      aria-expanded={open}
      aria-controls={controls}
      onClick={onToggle}
      title={open ? 'Hide the todo list' : 'Show the todo list'}
      className={`todo-action rounded-full border px-3 py-1.5 text-xs font-semibold shadow-sm ${
        open ? 'border-success bg-teal-tint text-success' : 'border-stroke bg-surface text-ink hover:bg-raised'
      }`}
    >
      ☑ Todo {done}/{todos.length}
    </button>
  )
}

// The chat's todo list (django_ergo.bots.todos): the model writes it and checks items off.
// Shown at the bottom of the chat when its TodoButton is on.
export function TodoList({ id, todos }: { id?: string; todos: Todo[] }) {
  if (!todos.length) return null
  return (
    <section
      id={id}
      aria-label="Todo list"
      className="todo-list mx-4 mt-2 flex max-h-48 flex-col gap-1 overflow-y-auto rounded-card border border-stroke bg-surface px-4 py-3 text-xs"
    >
      <div className="flex items-center gap-2 text-zinc-500">
        <span className="font-medium text-ink">Todo</span>
        <span>{todoProgress(todos)}</span>
      </div>
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
    </section>
  )
}
