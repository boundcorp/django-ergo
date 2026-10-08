import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import type { Pin } from '../api'
import { api } from '../api'
import { PageViewer } from '../components/Pins'

type Loaded = { state: 'loading' } | { state: 'missing' } | { state: 'ready'; pin: Pin; bot: string }

// A pinned page full-screen (/pages/view?session=<id>&pin=<path or attachment id>), where its bridge works
// as it does in the chat: the same PageViewer, with no sidebar.
export function PageView() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const sessionId = params.get('session') ?? ''
  const wanted = params.get('pin') ?? ''
  const [loaded, setLoaded] = useState<Loaded>({ state: 'loading' })

  useEffect(() => {
    let current = true
    setLoaded({ state: 'loading' })
    if (!sessionId || !wanted) {
      setLoaded({ state: 'missing' })
      return
    }
    Promise.all([api.pins(sessionId), api.session(sessionId)])
      .then(([pins, detail]) => {
        if (!current) return
        const pin = pins.find(p => (p.path ?? p.id) === wanted)
        setLoaded(pin ? { state: 'ready', pin, bot: detail.session.bot } : { state: 'missing' })
      })
      .catch(() => current && setLoaded({ state: 'missing' }))
    return () => {
      current = false
    }
  }, [sessionId, wanted])

  if (loaded.state === 'loading') return <div className="p-6 text-sm text-zinc-500">Loading…</div>
  if (loaded.state === 'missing') {
    return (
      <div className="p-6 text-sm text-zinc-500">
        That page isn't pinned in this chat any more.{' '}
        {sessionId && (
          <button className="underline" onClick={() => navigate(`/s/${sessionId}`)}>
            Back to chat
          </button>
        )}
      </div>
    )
  }
  return (
    <div className="flex h-screen flex-col">
      <PageViewer
        pin={loaded.pin}
        bot={loaded.bot}
        sessionId={sessionId}
        refreshKey={null}
        standalone
        onClose={() => navigate(`/s/${sessionId}`)}
      />
    </div>
  )
}
