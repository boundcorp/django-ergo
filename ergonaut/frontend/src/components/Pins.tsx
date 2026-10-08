import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Pin } from '../api'
import { api } from '../api'
import { PageBridge, eventsUrl } from '../pageBridge'
import type { ToastKind } from '../pageBridge'
import { themed, useTheme } from '../theme'

const TYPE_ICONS: [RegExp, string][] = [
  [/\.(jhtml|html?)$/i, '📊'],
  [/\.(png|jpe?g|gif|webp|svg)$/i, '🖼️'],
  [/\.pdf$/i, '📕'],
  [/\.(md|txt)$/i, '📝'],
  [/\.(csv|json|xlsx?)$/i, '📋'],
  [/\.(mp4|webm|mov|mp3|wav)$/i, '🎞️'],
]

/** A pin's icon: its own, or one for its file type. */
export function pinIcon(pin: { icon?: string; filename?: string; path?: string; name: string }): string {
  if (pin.icon) return pin.icon
  const file = pin.path ?? pin.filename ?? pin.name
  return TYPE_ICONS.find(([pattern]) => pattern.test(file))?.[1] ?? '📄'
}

// The chat's pinned files, as a strip of tabs under the header.
export function Pins({
  sessionId,
  refreshKey,
  open,
  onOpen,
  onCount,
}: {
  sessionId: string
  refreshKey: unknown
  open: Pin | null
  onOpen: (pin: Pin | null) => void
  onCount?: (count: number) => void
}) {
  const [pins, setPins] = useState<Pin[]>([])

  useEffect(() => {
    let current = true
    api
      .pins(sessionId)
      .then(list => {
        if (!current) return
        setPins(list)
        onCount?.(list.length)
      })
      .catch(() => {
        if (!current) return
        setPins([])
        onCount?.(0)
      })
    return () => {
      current = false
    }
  }, [sessionId, refreshKey, onCount])

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
            className={`rounded-full border px-2.5 py-0.5 text-xs disabled:opacity-50 ${active ? 'border-indigo-500 bg-indigo-50 text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300' : 'border-zinc-300 text-zinc-700 hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-400 dark:hover:bg-zinc-900'}`}
            onClick={() => onOpen(active ? null : pin)}
          >
            {pinIcon(pin)} {pin.name}
          </button>
        )
      })}
    </div>
  )
}

type Toast = { id: number; text: string; kind: ToastKind; href?: string }
type ConfirmRequest = { preview: string; resolve: (yes: boolean) => void }

// A pinned page or file, live: reloads when the chat changes (new rows may have landed), calls the
// bot's page actions for the page, and re-renders when the tables it reads change (see pageBridge.ts).
export function PageViewer(props: {
  pin: Pin
  bot: string
  sessionId: string
  refreshKey: unknown
  onClose: () => void
  standalone?: boolean // the full-screen route: no "Open in a new tab"
}) {
  // A fresh viewer per pin, so one page's stream and pending dialogs never carry over to the next.
  return <PageFrame key={props.pin.url} {...props} />
}

function PageFrame({
  pin,
  bot,
  sessionId,
  refreshKey,
  onClose,
  standalone,
}: {
  pin: Pin
  bot: string
  sessionId: string
  refreshKey: unknown
  onClose: () => void
  standalone?: boolean
}) {
  const theme = useTheme()
  const iframe = useRef<HTMLIFrameElement>(null)
  const [nonce, setNonce] = useState(0)
  const [live, setLive] = useState(false)
  const [streamKey, setStreamKey] = useState('')
  const [toasts, setToasts] = useState<Toast[]>([])
  const [confirms, setConfirms] = useState<ConfirmRequest[]>([])
  const toastId = useRef(0)
  // What a page action is called with: the pin's path (bot folder) or the attachment id (chat file).
  const page = pin.path ?? pin.id ?? ''

  const notify = useCallback((text: string, kind: ToastKind, href?: string) => {
    const id = (toastId.current += 1)
    setToasts(list => [...list, { id, text, kind, href }])
    window.setTimeout(() => setToasts(list => list.filter(t => t.id !== id)), 5000)
  }, [])

  const bridge = useMemo(
    () =>
      new PageBridge({
        bot,
        page,
        sessionId,
        frameWindow: () => iframe.current?.contentWindow ?? null,
        // The page is sandboxed (opaque origin), so "*" is the only target origin that reaches it.
        postToPage: message => iframe.current?.contentWindow?.postMessage(message, '*'),
        callAction: api.pageAction,
        confirm: preview => new Promise<boolean>(resolve => setConfirms(list => [...list, { preview, resolve }])),
        notify,
        openUrl: url => window.open(url, '_blank', 'noopener,noreferrer'),
        reloadFrame: () => setNonce(n => n + 1),
        onStreamTables: tables => setStreamKey(tables.join(',')),
      }),
    [bot, page, sessionId, notify],
  )
  useEffect(() => () => bridge.dispose(), [bridge])

  useEffect(() => {
    const onMessage = (event: MessageEvent) => void bridge.handleMessage(event.source, event.data)
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [bridge])

  // One stream per open page, for the tables it reported.
  useEffect(() => {
    if (!streamKey) return
    const source = new EventSource(eventsUrl(bot, streamKey.split(',')))
    source.onopen = () => setLive(true)
    source.onerror = () => setLive(false) // the browser reconnects and resumes from the last event id
    source.onmessage = event => bridge.handleStreamData(event.data)
    return () => {
      source.close()
      setLive(false)
    }
  }, [bot, bridge, streamKey])

  // New chat content re-renders the page.
  const shownKey = useRef(refreshKey)
  useEffect(() => {
    if (Object.is(shownKey.current, refreshKey)) return
    shownKey.current = refreshKey
    bridge.frameReplaced()
    setNonce(n => n + 1)
  }, [refreshKey, bridge])

  // Leaving the page counts as "No" for any approval still waiting.
  const waiting = useRef(confirms)
  waiting.current = confirms
  useEffect(() => () => waiting.current.forEach(c => c.resolve(false)), [])

  const answer = (yes: boolean) => {
    const [first] = confirms
    if (!first) return
    setConfirms(list => list.slice(1))
    first.resolve(yes)
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-3 border-b border-zinc-200 px-6 py-1.5 text-xs dark:border-zinc-800">
        <span className="font-medium">{pin.name}</span>
        <span
          role="status"
          aria-label={live ? 'Live' : 'Not live'}
          title={live ? 'Live: this page refreshes when its tables change' : 'Not live'}
          className="flex items-center gap-1 text-zinc-500"
        >
          <span className={`inline-block h-2 w-2 rounded-full ${live ? 'bg-green-500' : 'bg-zinc-400'}`} />
          {live && 'Live'}
        </span>
        <button
          className="text-zinc-500 underline"
          onClick={() => {
            bridge.frameReplaced()
            setNonce(n => n + 1)
          }}
        >
          Reload
        </button>
        {!standalone && (
          <a
            className="text-zinc-500 underline"
            href={`/pages/view?session=${encodeURIComponent(sessionId)}&pin=${encodeURIComponent(page)}`}
            target="_blank"
            rel="noreferrer"
          >
            Open in a new tab
          </a>
        )}
        <button className="ml-auto text-zinc-500 hover:text-zinc-900 dark:hover:text-zinc-100" onClick={onClose}>
          Back to chat ✕
        </button>
      </div>
      <iframe
        ref={iframe}
        key={`${nonce}:${theme}`}
        title={pin.name}
        src={themed(pin.url, theme)}
        className="min-h-0 w-full flex-1"
      />
      {toasts.length > 0 && (
        <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex flex-col gap-2" aria-live="polite">
          {toasts.map(t => (
            <div
              key={t.id}
              className={`rounded-md px-3 py-2 text-sm shadow-lg ${t.kind === 'error' ? 'bg-red-600 text-white' : 'bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900'}`}
            >
              {t.text}
              {t.href && (
                <>
                  {' '}
                  <a className="pointer-events-auto underline" href={t.href}>
                    Open
                  </a>
                </>
              )}
            </div>
          ))}
        </div>
      )}
      {confirms[0] && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
          <div
            role="dialog"
            aria-modal="true"
            aria-label="Confirm action"
            className="w-full max-w-md rounded-lg bg-white p-4 shadow-xl dark:bg-zinc-900"
          >
            <p className="whitespace-pre-wrap text-sm">{confirms[0].preview}</p>
            <div className="mt-4 flex justify-end gap-2">
              <button
                className="rounded border border-zinc-300 px-3 py-1 text-sm dark:border-zinc-700"
                onClick={() => answer(false)}
              >
                No
              </button>
              <button className="rounded bg-indigo-600 px-3 py-1 text-sm text-white" onClick={() => answer(true)}>
                Yes
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
