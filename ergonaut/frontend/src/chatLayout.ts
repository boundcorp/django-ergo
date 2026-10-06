/** Counts and labels for the phone session bar. Pure so the live bar and tests share one rule. */

export type StatusRequest = { direction: string; status: string }
export type StatusWorker = { status: string }

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
export function statusSummary(requests: StatusRequest[], workers: StatusWorker[], pinCount = 0): string {
  const parts: string[] = []
  const waiting = waitingCount(requests)
  const workingFor = workingForCount(requests)
  const running = runningWorkerCount(workers)
  if (waiting) parts.push(countPhrase(waiting, 'waiting', 'waiting'))
  if (workingFor) parts.push(countPhrase(workingFor, 'working for you', 'working for you'))
  if (running) parts.push(countPhrase(running, 'worker running', 'workers running'))
  if (pinCount > 0) parts.push(countPhrase(pinCount, 'pinned', 'pinned'))
  return parts.join(' · ')
}

type SuggestionBlock = { type: string; name?: string; input?: unknown }

/** Suggestions stored on the latest send_reply tool call, when the turn response has none. */
export function suggestionsFromMessages(messages: { blocks: SuggestionBlock[] }[]): unknown {
  for (let i = messages.length - 1; i >= 0; i--) {
    const blocks = messages[i].blocks
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
