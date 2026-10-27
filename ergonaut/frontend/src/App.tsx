import { Component, useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { BrowserRouter, Link, Navigate, Route, Routes, useLocation, useParams } from 'react-router-dom'
import type { Bot, Session, SidebarPin, User, Worker } from './api'
import { ApiError, api } from './api'
import { Sidebar } from './components/Sidebar'
import { Chat } from './pages/Chat'
import { Login } from './pages/Login'
import { Sessions } from './pages/Sessions'
import { BotPage } from './pages/BotPage'
import { PageView } from './pages/PageView'
import { NewThread } from './pages/NewThread'
import { Memory } from './pages/Memory'
import { CostsPage } from './pages/Costs'
import { RoutingPage } from './pages/Routing'
import { ThreadsPage } from './pages/ThreadsPage'
import { ApiKeysPage } from './pages/ApiKeys'
import { AccountMenu, WorkerStatus } from './components/ShellControls'
import { UsageStatus } from './components/UsageStatus'
import { useVisualViewport } from './viewport'
import { topbarLocation } from './topbar'
import './chat-mobile.css'
import { Home } from './pages/Home'
import type { DirectoryState } from './components/DashboardWorkspace'
import { DirectoryContext } from './components/BotIcon'

function App() {
  const [user, setUser] = useState<User | null | undefined>(undefined)
  const [bots, setBots] = useState<Bot[]>([])
  const [sessions, setSessions] = useState<Session[]>([])
  const [pins, setPins] = useState<Record<string, SidebarPin[]>>({})
  const [botErrors, setBotErrors] = useState<{ folder: string; name: string; error: string }[]>([])
  const [workers, setWorkers] = useState<Worker[]>([])
  const [workerState, setWorkerState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [directoryState, setDirectoryState] = useState<DirectoryState>('loading')

  useEffect(() => {
    api
      .csrf()
      .then(() => api.me())
      .then(setUser)
      .catch(e => (e instanceof ApiError && e.status === 401 ? setUser(null) : setUser(null)))
  }, [])

  const refresh = useCallback(async () => {
    try {
      const [b, s, p, e, workerResult] = await Promise.all([
        api.bots(),
        api.sessions(),
        api.allPins().catch(() => ({})),
        api.botErrors().catch(() => []),
        api.activeWorkers().then(
          value => ({ value, error: false }),
          () => ({ value: [] as Worker[], error: true }),
        ),
      ])
      setBots(b)
      setSessions(s)
      setPins(p)
      setBotErrors(e)
      setWorkers(workerResult.value)
      setWorkerState(workerResult.error ? 'error' : 'ready')
      setDirectoryState('ready')
    } catch (error) {
      setDirectoryState('error')
      throw error
    }
  }, [])

  useEffect(() => {
    if (user) refresh().catch(() => {})
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
        <Routes>
          {/* The full-screen page viewer has no sidebar or top bar. */}
          <Route path="/pages/view" element={<PageView />} />
          <Route
            path="*"
            element={
              <Shell
                user={user}
                bots={bots}
                sessions={sessions}
                pins={pins}
                botErrors={botErrors}
                workers={workers}
                workerState={workerState}
                refresh={refresh}
                directoryState={directoryState}
                onSignOut={() => api.logout().finally(() => setUser(null))}
              />
            }
          />
        </Routes>
      </DirectoryContext.Provider>
    </BrowserRouter>
  )
}

// Below 640px the sidebar is a modal drawer opened from the top bar.
function Shell({
  user,
  bots,
  sessions,
  pins,
  botErrors,
  workers,
  workerState,
  refresh,
  directoryState,
  onSignOut,
}: {
  user: User
  bots: Bot[]
  sessions: Session[]
  pins: Record<string, SidebarPin[]>
  botErrors: { folder: string; name: string; error: string }[]
  workers: Worker[]
  workerState: 'loading' | 'ready' | 'error'
  refresh: () => Promise<void>
  directoryState: DirectoryState
  onSignOut: () => void
}) {
  const [navOpen, setNavOpen] = useState(false)
  const trigger = useRef<HTMLButtonElement>(null)
  const drawer = useRef<HTMLElement>(null)
  const { pathname } = useLocation()
  const { crumbs, title } = topbarLocation(pathname, sessions)
  useVisualViewport()

  const closeDrawer = useCallback((restoreFocus = false) => {
    setNavOpen(false)
    if (restoreFocus) requestAnimationFrame(() => trigger.current?.focus())
  }, [])

  useEffect(() => closeDrawer(), [pathname, closeDrawer])
  useEffect(() => {
    if (!navOpen) return
    const focusable = () => [
      ...(drawer.current?.querySelectorAll<HTMLElement>(
        'a, button, input, select, textarea, [tabindex]:not([tabindex="-1"])',
      ) ?? []),
    ]
    focusable()[0]?.focus()
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        closeDrawer(true)
        return
      }
      if (event.key !== 'Tab') return
      const items = focusable()
      if (!items.length) return
      const first = items[0]
      const last = items[items.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', keydown)
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', keydown)
      document.body.style.overflow = previousOverflow
    }
  }, [navOpen, closeDrawer])

  return (
    <div className="app-shell flex">
      {navOpen && (
        <button
          type="button"
          className="drawer-backdrop"
          aria-label="Close navigation menu"
          onClick={() => closeDrawer(true)}
        />
      )}
      <aside
        ref={drawer}
        id="app-sidebar"
        className="app-sidebar shrink-0"
        data-open={navOpen}
        role={navOpen ? 'dialog' : undefined}
        aria-modal={navOpen || undefined}
        aria-label={navOpen ? 'Navigation menu' : undefined}
      >
        <Sidebar bots={bots} sessions={sessions} pins={pins} botErrors={botErrors} onChange={refresh} />
      </aside>
      <main className="app-main min-w-0 flex-1">
        <div className="app-topbar flex items-center gap-4">
          <button
            ref={trigger}
            type="button"
            className="nav-toggle"
            aria-controls="app-sidebar"
            aria-expanded={navOpen}
            aria-label={navOpen ? 'Close navigation menu' : 'Open navigation menu'}
            onClick={() => setNavOpen(open => !open)}
          >
            {navOpen ? 'Close' : 'Menu'}
          </button>
          <Link to="/" className="topbar-brand">
            ERGONAUT_
          </Link>
          <nav className="topbar-path" aria-label="Breadcrumb">
            {crumbs.map(crumb => (
              <span key={crumb.label} className="topbar-crumb">
                {crumb.to ? <Link to={crumb.to}>{crumb.label}</Link> : crumb.label}
                <span aria-hidden="true"> /</span>
              </span>
            ))}
            <span className="topbar-title" aria-current="page">
              {title}
            </span>
          </nav>
          <div className="topbar-controls ml-auto flex items-center gap-2">
            <UsageStatus />
            <WorkerStatus data={{ state: workerState, workers }} onRetry={() => refresh().catch(() => {})} />
            <AccountMenu user={user} onSignOut={onSignOut} />
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-hidden">
          <PageErrorBoundary key={pathname}>
            <Routes>
              <Route
                path="/"
                element={
                  <Home
                    user={user}
                    bots={bots}
                    sessions={sessions}
                    pins={pins}
                    state={directoryState}
                    onRetry={() => refresh().catch(() => {})}
                  />
                }
              />
              <Route
                path="/threads"
                element={
                  <ThreadsPage
                    user={user}
                    bots={bots}
                    sessions={sessions}
                    pins={pins}
                    state={directoryState}
                    onChange={refresh}
                  />
                }
              />
              <Route path="/s/:id" element={<ChatRoute onChange={refresh} />} />
              <Route path="/sessions" element={<Sessions bots={bots} />} />
              <Route path="/costs" element={<CostsPage />} />
              <Route path="/routing" element={<RoutingPage />} />
              <Route path="/api-keys" element={<ApiKeysPage />} />
              <Route path="/bots/:name" element={<BotPage />} />
              <Route path="/bots/:name/new-thread" element={<NewThread onChange={refresh} />} />
              <Route path="/bots/:name/kb" element={<Memory />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </PageErrorBoundary>
        </div>
      </main>
    </div>
  )
}

export default App

// A fresh Chat per session, so per-chat state (queued turn, attachments, draft) never carries over.
function ChatRoute({ onChange }: { onChange: () => void }) {
  const { id = '' } = useParams()
  return <Chat key={id} onChange={onChange} />
}

// A page that throws while rendering shows its error here, and the rest of the app stays up,
// instead of React unmounting everything and leaving a blank screen.
class PageErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error) {
    console.error(error)
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children
    return (
      <div className="page-content h-full overflow-y-auto">
        <h1 className="page-title">This page hit an error</h1>
        <pre className="mt-4 whitespace-pre-wrap break-words rounded-card border border-danger/40 bg-red-tint p-3 text-sm text-danger">
          {error.message || String(error)}
        </pre>
        <button
          className="mt-4 rounded-control border border-stroke px-3 py-2 text-sm"
          onClick={() => window.location.reload()}
        >
          Reload
        </button>
      </div>
    )
  }
}
