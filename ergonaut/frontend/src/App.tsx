import { useCallback, useEffect, useState } from 'react'
import { BrowserRouter, Navigate, Route, Routes, useParams } from 'react-router-dom'
import type { Bot, Session, User } from './api'
import { ApiError, api } from './api'
import { Sidebar } from './components/Sidebar'
import { Chat } from './pages/Chat'
import { Login } from './pages/Login'
import { Sessions } from './pages/Sessions'
import { BotPage } from './pages/BotPage'
import { NewThread } from './pages/NewThread'
import { Memory } from './pages/Memory'
import { CostsPage } from './pages/Costs'
import { ThemeToggle } from './theme'

function Home({ bots }: { bots: Bot[] }) {
  const root = bots.find(b => b.root_session_id)
  if (root) return <Navigate to={`/s/${root.root_session_id}`} replace />
  return (
    <div className="page-content">
      <h1 className="page-title mb-4">Ergonaut</h1>
      <div className="surface-card p-8 text-zinc-500">
        {bots.length ? 'Pick a bot on the left and start chatting.' : 'No bots are loaded yet.'}
      </div>
    </div>
  )
}

function App() {
  const [user, setUser] = useState<User | null | undefined>(undefined)
  const [bots, setBots] = useState<Bot[]>([])
  const [sessions, setSessions] = useState<Session[]>([])
  const [pins, setPins] = useState<Record<string, { name: string; url: string }[]>>({})
  const [botErrors, setBotErrors] = useState<{ folder: string; name: string; error: string }[]>([])

  useEffect(() => {
    api
      .csrf()
      .then(() => api.me())
      .then(setUser)
      .catch(e => (e instanceof ApiError && e.status === 401 ? setUser(null) : setUser(null)))
  }, [])

  const refresh = useCallback(async () => {
    const [b, s, p, e] = await Promise.all([
      api.bots(),
      api.sessions(),
      api.allPins().catch(() => ({})),
      api.botErrors().catch(() => []),
    ])
    setBots(b)
    setSessions(s)
    setPins(p)
    setBotErrors(e)
  }, [])

  useEffect(() => {
    if (user) refresh().catch(() => setBots([]))
  }, [user, refresh])

  // Keep the sidebar's busy spinners current: poll quickly while a turn runs, slowly otherwise.
  const anyBusy = sessions.some(s => s.busy)
  useEffect(() => {
    if (!user) return
    const timer = setInterval(() => refresh().catch(() => {}), anyBusy ? 3000 : 20000)
    return () => clearInterval(timer)
  }, [user, refresh, anyBusy])

  if (user === undefined) return null
  if (user === null) return <Login onLogin={setUser} />

  return (
    <BrowserRouter>
      <div className="app-shell flex h-screen">
        <aside className="app-sidebar shrink-0">
          <Sidebar bots={bots} sessions={sessions} pins={pins} botErrors={botErrors} onChange={refresh} />
        </aside>
        <main className="app-main min-w-0 flex-1">
          <div className="app-topbar flex items-center gap-4">
            <span className="text-sm text-muted">{user.first_name || user.username}</span>
            <span className="ml-auto">
              <ThemeToggle />
            </span>
          </div>
          <div className="min-h-0 flex-1 overflow-hidden">
            <Routes>
              <Route path="/" element={<Home bots={bots} />} />
              <Route path="/s/:id" element={<ChatRoute onChange={refresh} />} />
              <Route path="/sessions" element={<Sessions bots={bots} />} />
              <Route path="/costs" element={<CostsPage />} />
              <Route path="/bots/:name" element={<BotPage />} />
              <Route path="/bots/:name/new-thread" element={<NewThread onChange={refresh} />} />
              <Route path="/bots/:name/kb" element={<Memory />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </div>
        </main>
      </div>
    </BrowserRouter>
  )
}

export default App

// A fresh Chat per session, so per-chat state (queued turn, attachments, draft) never carries over.
function ChatRoute({ onChange }: { onChange: () => void }) {
  const { id = '' } = useParams()
  return <Chat key={id} onChange={onChange} />
}
