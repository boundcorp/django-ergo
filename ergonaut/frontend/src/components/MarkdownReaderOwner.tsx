import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { Reader } from './MarkdownAttachment'

type Request = { id: string; label: string; opener: HTMLElement | null }
type Marker = {
  kind: 'ergonaut-markdown-reader'
  owner: string
  token: string
  id: string
  label: string
  original: unknown
}
const ReaderContext = createContext<(id: string, label: string, opener: HTMLElement) => void>(() => {
  throw new Error('MarkdownReaderOwner is required')
})

export const useMarkdownReader = () => useContext(ReaderContext)

const registryKey = (token: string) => `ergonaut.markdown-reader.${token}`

let idSequence = 0
function readerId(): string {
  const bytes = new Uint8Array(16)
  try {
    globalThis.crypto.getRandomValues(bytes)
    return `${Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')}-${++idSequence}`
  } catch {
    // These IDs identify history entries, not credentials. Retain a per-page
    // sequence as well as time and randomness when Web Crypto is unavailable.
    return `${Date.now().toString(36)}-${++idSequence}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`
  }
}

function registered(marker: Marker): boolean {
  try {
    return sessionStorage.getItem(registryKey(marker.token)) === marker.owner
  } catch {
    return false
  }
}

function markerIn(state: unknown, owner: string, requests: Map<string, Request>): Marker | undefined {
  if (!state || typeof state !== 'object') return undefined
  return Object.entries(state).find(
    ([key, value]) =>
      key.startsWith('__ergonautMarkdownReader') &&
      value &&
      typeof value === 'object' &&
      value.kind === 'ergonaut-markdown-reader' &&
      typeof value.owner === 'string' &&
      typeof value.token === 'string' &&
      typeof value.id === 'string' &&
      typeof value.label === 'string' &&
      Object.prototype.hasOwnProperty.call(value, 'original') &&
      ((value.owner === owner && requests.has(value.token)) || registered(value as Marker)),
  )?.[1] as Marker | undefined
}

export function MarkdownReaderOwner({ children }: { children: ReactNode }) {
  const location = useLocation()
  const navigate = useNavigate()
  const [owner] = useState(readerId)
  const requests = useRef(new Map<string, Request>())
  const [mobile, setMobile] = useState<(Request & { locationKey: string }) | null>(null)
  const marker = markerIn(location.state, owner, requests.current)
  const owned = marker?.owner === owner && requests.current.has(marker.token) ? marker : undefined
  const request = owned ? requests.current.get(owned.token) : mobile?.locationKey === location.key ? mobile : undefined
  const previous = useRef<Request | undefined>(undefined)

  useEffect(() => {
    setMobile(current => (current && current.locationKey !== location.key ? null : current))
  }, [location.key])

  useEffect(() => {
    // A marker from a previous page lifetime has no provably owned predecessor.
    // Restore the exact original state rather than navigating to an unknown entry.
    if (marker && !owned) {
      navigate(
        { pathname: location.pathname, search: location.search, hash: location.hash },
        { replace: true, state: marker.original },
      )
    }
  }, [marker, owned, location.pathname, location.search, location.hash, navigate])

  useEffect(() => {
    const old = previous.current
    previous.current = request
    if (old && !request) {
      if (old.opener?.isConnected) old.opener.focus({ preventScroll: true })
      else {
        const fallback = document.querySelector<HTMLElement>('main') ?? document.body
        const hadTabindex = fallback.hasAttribute('tabindex')
        if (!hadTabindex) fallback.setAttribute('tabindex', '-1')
        fallback.focus({ preventScroll: true })
        if (!hadTabindex) fallback.addEventListener('blur', () => fallback.removeAttribute('tabindex'), { once: true })
      }
    }
  }, [request])

  const open = useCallback(
    (id: string, label: string, opener: HTMLElement) => {
      const next = { id, label, opener }
      if (window.matchMedia('(max-width: 600px)').matches) {
        setMobile({ ...next, locationKey: location.key })
        return
      }
      let token = readerId()
      while (requests.current.has(token)) token = readerId()
      try {
        sessionStorage.setItem(registryKey(token), owner)
      } catch {
        // Without session storage, never identify an entry from a previous page lifetime.
      }
      requests.current.set(token, next)
      const original = location.state
      const state: Record<string, unknown> =
        original && typeof original === 'object' && !Array.isArray(original) ? { ...original } : {}
      let key = '__ergonautMarkdownReader'
      while (Object.prototype.hasOwnProperty.call(state, key)) key += '_'
      state[key] = {
        kind: 'ergonaut-markdown-reader',
        owner,
        token,
        id,
        label,
        original,
      } satisfies Marker
      navigate({ pathname: location.pathname, search: location.search, hash: location.hash }, { state })
    },
    [location, navigate, owner],
  )

  const close = useCallback(() => {
    if (owned) navigate(-1)
    else setMobile(null)
  }, [navigate, owned])

  return (
    <ReaderContext.Provider value={open}>
      {children}
      {request && <Reader key={owned?.token ?? 'mobile'} id={request.id} label={request.label} onClose={close} />}
    </ReaderContext.Provider>
  )
}
