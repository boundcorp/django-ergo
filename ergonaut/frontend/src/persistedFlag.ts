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

export function usePersistedFlag(key: string): [boolean, (open: boolean) => void] {
  const [open, setOpen] = useState(() => readFlag(key))
  const set = useCallback(
    (value: boolean) => {
      setOpen(value)
      saveFlag(key, value)
    },
    [key],
  )
  return [open, set]
}
