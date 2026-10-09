// Links to one message of a chat: /s/<session id>#m-<line>. The line is the message's sequence,
// the same number the history tools (ergo_chat_history_*) and `ergonaut-remote show` use.

export function messageAnchor(line: number): string {
  return `m-${line}`
}

export function messageLink(sessionId: string, line: number, origin = window.location.origin): string {
  return `${origin}/s/${sessionId}#${messageAnchor(line)}`
}

/** The line a location hash (#m-12) points at, if it points at one. */
export function linkedLine(hash: string): number | null {
  const match = /^#m-(\d+)$/.exec(hash)
  return match ? Number(match[1]) : null
}
