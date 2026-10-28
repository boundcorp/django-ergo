/** Counts and labels for the phone session bar. Pure so the live bar and tests share one rule. */

export type StatusRequest = { direction: string; status: string }
export type StatusWorker = { status: string }
export type StatusTodo = { status: string }

const CLOSED_REQUEST: Record<string, true> = { answered: true, failed: true }

/** Outgoing requests still open: the "Waiting on …" rows. */
export function waitingCount(requests: StatusRequest[]): number {
  return requests.filter(request => request.direction === 'out' && !CLOSED_REQUEST[request.status]).length
}

/** Incoming requests still open: the "Working for …" rows. */
export function workingForCount(requests: StatusRequest[]): number {
  return requests.filter(request => request.direction === 'in' && !CLOSED_REQUEST[request.status]).length
}

export function runningWorkerCount(workers: StatusWorker[]): number {
  return workers.filter(worker => worker.status === 'queued' || worker.status === 'running').length
}

function countPhrase(count: number, singular: string, plural: string): string {
  return `${count} ${count === 1 ? singular : plural}`
}

/** One line for the compact bar, e.g. "2 waiting · 1 worker running". Empty when nothing is pending. */
export function statusSummary(
  requests: StatusRequest[],
  workers: StatusWorker[],
  pinCount = 0,
  todos: StatusTodo[] = [],
): string {
  const parts: string[] = []
  const waiting = waitingCount(requests)
  const workingFor = workingForCount(requests)
  const running = runningWorkerCount(workers)
  if (waiting) parts.push(countPhrase(waiting, 'waiting', 'waiting'))
  if (workingFor) parts.push(countPhrase(workingFor, 'working for you', 'working for you'))
  if (running) parts.push(countPhrase(running, 'worker running', 'workers running'))
  if (pinCount > 0) parts.push(countPhrase(pinCount, 'pinned', 'pinned'))
  // An unfinished todo list: how far along it is.
  const done = todos.filter(todo => todo.status === 'completed').length
  if (done < todos.length) parts.push(`${done}/${todos.length} todos done`)
  return parts.join(' · ')
}

type SuggestionBlock = { type: string; name?: string; input?: unknown }

/** Suggestions stored on the latest send_reply tool call, when the turn response has none. */
export function suggestionsFromMessages(messages: { role?: string; blocks: SuggestionBlock[] }[]): unknown {
  for (let i = messages.length - 1; i >= 0; i--) {
    const { blocks, role } = messages[i]
    if (role === 'user' && blocks.some(block => block.type === 'text' || block.type === 'attachment')) return undefined
    for (let j = blocks.length - 1; j >= 0; j--) {
      const block = blocks[j]
      if (block.type !== 'tool_use' || block.name !== 'send_reply') continue
      const input = block.input
      if (!input || typeof input !== 'object' || !('suggestions' in input)) return undefined
      return input.suggestions
    }
  }
  return undefined
}
