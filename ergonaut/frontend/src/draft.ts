import { useEffect, useState } from 'react'

// Text typed into a chat box, kept per chat in this browser until it is sent.
const PREFIX = 'ergonaut.draft.'

function read(key: string): string {
  try {
    return localStorage.getItem(PREFIX + key) ?? ''
  } catch {
    return ''
  }
}

function write(key: string, text: string) {
  try {
    if (text) localStorage.setItem(PREFIX + key, text)
    else localStorage.removeItem(PREFIX + key)
  } catch {
    // storage unavailable or full: the draft lasts this page view
  }
}

// Forget a draft now, for a box that is about to go away (its own save would not run).
export function clearDraft(key: string) {
  write(key, '')
}

// Like useState(''), but the text is saved under `key` and restored when the box comes back.
// Setting it to '' (on send) forgets the draft.
export function useDraft(key: string): [string, (text: string | ((current: string) => string)) => void] {
  const [draft, setDraft] = useState(() => ({ key, text: read(key) }))
  // The same component can move to another chat (a route param changes): load that chat's draft.
  if (draft.key !== key) setDraft({ key, text: read(key) })
  useEffect(() => write(draft.key, draft.text), [draft])
  function setText(text: string | ((current: string) => string)) {
    setDraft(current => ({ key: current.key, text: typeof text === 'function' ? text(current.text) : text }))
  }
  return [draft.key === key ? draft.text : read(key), setText]
}
