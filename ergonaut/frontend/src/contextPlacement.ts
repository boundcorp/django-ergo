import type { Call, Message } from './api'

/** Place context after each call's final loaded assistant message. Messages are sequence-ordered.
 * Calls keep their input priority: an overlapping earlier call owns the message even when it has
 * no context, or its final assistant is elsewhere. Unfinished calls extend through loaded history.
 * Binary searches locate each range; successor links skip messages already claimed by earlier calls,
 * so overlapping ranges cannot turn this back into a scan of the transcript for every call. */
export function contextPlacement(messages: Message[], calls: Call[]): Map<number, Call> {
  const placed = new Map<number, Call>()
  if (!calls.length) return placed
  const assistants = messages.filter(message => message.role === 'assistant')
  const next = Array.from({ length: assistants.length + 1 }, (_, index) => index)

  function unclaimed(index: number): number {
    let root = index
    while (next[root] !== root) root = next[root]
    while (index !== root) {
      const parent = next[index]
      next[index] = root
      index = parent
    }
    return root
  }

  function boundary(line: number, inclusive: boolean): number {
    let low = 0
    let high = assistants.length
    while (low < high) {
      const middle = low + Math.floor((high - low) / 2)
      if (assistants[middle].line < line || (inclusive && assistants[middle].line === line)) low = middle + 1
      else high = middle
    }
    return low
  }

  for (const call of calls) {
    if (call.first_sequence == null) continue
    const start = boundary(call.first_sequence, false)
    const end = boundary(call.last_sequence ?? Infinity, true)
    if (start >= end) continue
    // Being the last message owned by this call is not enough: it must be the last in its whole range.
    if (unclaimed(end - 1) === end - 1) placed.set(assistants[end - 1].line, call)
    for (let index = unclaimed(start); index < end; index = unclaimed(index)) {
      next[index] = unclaimed(index + 1)
    }
  }
  return placed
}
