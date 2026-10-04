import { useCallback, useEffect, useState } from 'react'
import { BrowserRouter, Link, Navigate, Route, Routes, useParams } from 'react-router-dom'
import type { Bot, Session, SidebarPin, User } from './api'
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
import { DirectoryContext } from './components/BotIcon'

function Home({ bots, sessions }: { bots: Bot[]; sessions: Session[] }) {
  const recent = sessions.slice(0, 4)
  return (
    <div className="page-content h-full overflow-y-auto">
      <p className="eyebrow">Your workspace</p>
      <h1 className="page-title mt-3">Good to see you</h1>
      <p className="page-lede mt-3">Pick up a conversation or start a focused thread with a bot.</p>
      <div className="mt-10 grid gap-8 lg:grid-cols-[1.15fr_0.85fr]">
        <section className="surface-card flex min-h-64 flex-col items-center justify-center px-6 py-10 text-center">
          <div className="h-16 w-16 rounded-panel bg-raised" aria-hidden="true" />
          <h2 className="mt-6 text-lg font-semibold">Start with a question</h2>
          <p className="mt-2 max-w-sm text-sm text-muted">
            Choose a bot or open a recent thread to continue your work.
          </p>
          {!!bots.length && (
            <div className="mt-6 flex flex-wrap justify-center gap-2">
              {bots.map(bot => (
                <Link
                  key={bot.name}
                  to={`/bots/${bot.name}/new-thread`}
                  className="rounded-control border border-accent px-3 py-2 text-sm font-semibold text-accent hover:bg-indigo-tint"
                >
                  New thread with {bot.name}
                </Link>
              ))}
            </div>
          )}
        </section>
        <section>
          <h2 className="font-display text-2xl font-bold">Recent threads</h2>
          {recent.length ? (
            <ul className="mt-5 space-y-3">
              {recent.map(session => (
                <li key={session.id}>
                  <Link
                    to={`/s/${session.id}`}
                    className="flex items-center gap-3 rounded-card border border-stroke bg-surface px-4 py-3 hover:border-accent"
                  >
                    <span
                      className={`h-3 w-3 shrink-0 rounded-full ${session.attention ? 'bg-warning' : session.busy ? 'bg-mint' : 'bg-accent'}`}
                      aria-hidden="true"
                    />
                    <span className="min-w-0 flex-1 truncate font-semibold">{session.title}</span>
                    <span className="text-xs text-muted">{session.bot}</span>
                  </Link>
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-5 text-sm text-muted">No recent threads yet.</p>
          )}
        </section>
      </div>
    </div>
  )
}

function App() {
  const [user, setUser] = useState<User | null | undefined>(undefined)
  const [bots, setBots] = useState<Bot[]>([])
  const [sessions, setSessions] = useState<Session[]>([])
  const [pins, setPins] = useState<Record<string, SidebarPin[]>>({})
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
      <DirectoryContext.Provider value={{ bots, sessions }}>
        <div className="app-shell flex h-screen">
          <aside className="app-sidebar shrink-0">
            <Sidebar bots={bots} sessions={sessions} pins={pins} botErrors={botErrors} onChange={refresh} />
          </aside>
          <main className="app-main min-w-0 flex-1">
            <div className="app-topbar flex items-center gap-4">
              <Link to="/" className="topbar-brand">
                ERGONAUT_
              </Link>
              <span className="topbar-crumb hidden sm:inline">Workspace</span>
              <span className="topbar-crumb hidden sm:inline">/</span>
              <span className="topbar-crumb hidden sm:inline">Your journey</span>
              <span className="ml-auto text-sm font-semibold text-ink">{user.first_name || user.username}</span>
              <ThemeToggle />
            </div>
            <div className="min-h-0 flex-1 overflow-hidden">
              <Routes>
                <Route path="/" element={<Home bots={bots} sessions={sessions} />} />
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
      </DirectoryContext.Provider>
    </BrowserRouter>
  )
}

export default App

// A fresh Chat per session, so per-chat state (queued turn, attachments, draft) never carries over.
function ChatRoute({ onChange }: { onChange: () => void }) {
  const { id = '' } = useParams()
  return <Chat key={id} onChange={onChange} />
}
