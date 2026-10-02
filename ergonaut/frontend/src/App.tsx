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

function Home({ bots }: { bots: Bot[] }) {
  const root = bots.find(b => b.root_session_id)
  if (root) return <Navigate to={`/s/${root.root_session_id}`} replace />
  return (
    <div className="p-6 text-zinc-500">
      {bots.length ? 'Pick a bot on the left and start chatting.' : 'No bots are loaded yet.'}
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

  if (user === undefined) return null
  if (user === null) return <Login onLogin={setUser} />

  return (
    <BrowserRouter>
      <div className="flex h-screen">
        <aside className="w-64 shrink-0 border-r border-zinc-200 dark:border-zinc-800">
          <Sidebar bots={bots} sessions={sessions} pins={pins} botErrors={botErrors} onChange={refresh} />
        </aside>
        <main className="min-w-0 flex-1">
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
