import { useCallback, useEffect, useState } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import type { Bot, Session, User } from './api'
import { ApiError, api } from './api'
import { Sidebar } from './components/Sidebar'
import { Chat } from './pages/Chat'
import { Login } from './pages/Login'
import { Sessions } from './pages/Sessions'
import { BotPage } from './pages/BotPage'
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

  useEffect(() => {
    api
      .csrf()
      .then(() => api.me())
      .then(setUser)
      .catch(e => (e instanceof ApiError && e.status === 401 ? setUser(null) : setUser(null)))
  }, [])

  const refresh = useCallback(async () => {
    const [b, s] = await Promise.all([api.bots(), api.sessions()])
    setBots(b)
    setSessions(s)
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
          <Sidebar bots={bots} sessions={sessions} onChange={refresh} />
        </aside>
        <main className="min-w-0 flex-1">
          <Routes>
            <Route path="/" element={<Home bots={bots} />} />
            <Route path="/s/:id" element={<Chat onChange={refresh} />} />
            <Route path="/sessions" element={<Sessions bots={bots} />} />
            <Route path="/costs" element={<CostsPage />} />
            <Route path="/bots/:name" element={<BotPage />} />
            <Route path="/bots/:name/kb" element={<Memory />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  )
}

export default App
