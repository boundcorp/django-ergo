import { useEffect, useState } from 'react'
import type { Pin } from '../api'
import { api } from '../api'

// The chat's pinned files, as a strip of tabs under the header.
export function Pins({
  sessionId,
  refreshKey,
  open,
  onOpen,
}: {
  sessionId: string
  refreshKey: unknown
  open: Pin | null
  onOpen: (pin: Pin | null) => void
}) {
  const [pins, setPins] = useState<Pin[]>([])

  useEffect(() => {
    api
      .pins(sessionId)
      .then(setPins)
      .catch(() => setPins([]))
  }, [sessionId, refreshKey])

  if (!pins.length) return null
  return (
    <div className="flex flex-wrap items-center gap-1.5 border-b border-zinc-200 px-6 py-2 dark:border-zinc-800">
      <span className="text-xs text-zinc-500">Pinned</span>
      {pins.map(pin => {
        const active = open?.url === pin.url
        return (
          <button
            key={pin.url}
            disabled={pin.exists === false}
            title={pin.exists === false ? `${pin.path} isn't in the bot folder yet` : (pin.path ?? pin.filename)}
            className={`rounded-full border px-2.5 py-0.5 text-xs disabled:opacity-50 ${active ? 'border-indigo-500 bg-indigo-50 text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300' : 'border-zinc-300 text-zinc-700 hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-900'}`}
            onClick={() => onOpen(active ? null : pin)}
          >
            📌 {pin.name}
          </button>
        )
      })}
    </div>
  )
}

// A pinned page or file, live: reloads when the chat changes (new rows may have landed).
export function PageViewer({ pin, refreshKey, onClose }: { pin: Pin; refreshKey: unknown; onClose: () => void }) {
  const [nonce, setNonce] = useState(0)
  useEffect(() => setNonce(n => n + 1), [refreshKey])
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-3 border-b border-zinc-200 px-6 py-1.5 text-xs dark:border-zinc-800">
        <span className="font-medium">{pin.name}</span>
        <button className="text-zinc-500 underline" onClick={() => setNonce(n => n + 1)}>
          Reload
        </button>
        <a className="text-zinc-500 underline" href={pin.url} target="_blank" rel="noreferrer">
          Open in a new tab
        </a>
        <button className="ml-auto text-zinc-500 hover:text-zinc-900 dark:hover:text-zinc-100" onClick={onClose}>
          Back to chat ✕
        </button>
      </div>
      <iframe key={nonce} title={pin.name} src={pin.url} className="min-h-0 w-full flex-1 bg-white" />
    </div>
  )
}
