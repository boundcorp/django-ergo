import { useCallback, useState } from 'react'

// An open/closed panel remembered per browser. Storage can be unavailable
// (private browsing), so it falls back to closed and forgets.

export function readFlag(key: string): boolean {
  try {
    return localStorage.getItem(key) === 'open'
  } catch {
    return false
  }
}

export function saveFlag(key: string, open: boolean) {
  try {
    localStorage.setItem(key, open ? 'open' : 'closed')
  } catch {
    // Storage unavailable.
  }
}

/** A remembered flag; when `key` changes (say, another chat's), it reads that key's choice. */
export function usePersistedFlag(key: string): [boolean, (open: boolean) => void] {
  const [state, setState] = useState(() => ({ key, open: readFlag(key) }))
  let open = state.open
  if (state.key !== key) {
    open = readFlag(key)
    setState({ key, open })
  }
  const set = useCallback(
    (value: boolean) => {
      setState({ key, open: value })
      saveFlag(key, value)
    },
    [key],
  )
  return [open, set]
}
