import { useState } from 'react'
import { Link, NavLink, useNavigate } from 'react-router-dom'
import type { Bot, Session } from '../api'
import { api } from '../api'

function SessionLink({ session, nested }: { session: Session; nested?: boolean }) {
  return (
    <NavLink
      to={`/s/${session.id}`}
      className={({ isActive }) =>
        `flex items-center gap-2 truncate rounded-md px-2 py-1 text-sm ${nested ? 'ml-4' : ''} ${
          isActive ? 'bg-zinc-200 font-medium dark:bg-zinc-800' : 'hover:bg-zinc-100 dark:hover:bg-zinc-900'
        }`
      }
    >
      <span
        title={
          session.open_in
            ? 'Working on a delegated request'
            : session.open_out
              ? 'Waiting on another thread'
              : undefined
        }
        className={`h-1.5 w-1.5 shrink-0 rounded-full ${
          session.open_in
            ? 'animate-pulse bg-amber-500'
            : session.status === 'completed'
              ? 'bg-zinc-400'
              : 'bg-emerald-500'
        }`}
      />
      <span className="truncate">{session.title}</span>
      {!!session.open_out && (
        <span className="ml-auto shrink-0 text-xs text-zinc-400" title="Waiting on other threads">
          ⏳{session.open_out > 1 ? session.open_out : ''}
        </span>
      )}
    </NavLink>
  )
}

// A chat's pinned files under it; each opens in that chat's viewer.
function PinLinks({
  session,
  pins,
  nested,
}: {
  session: Session
  pins?: { name: string; url: string }[]
  nested?: boolean
}) {
  if (!pins?.length) return null
  return (
    <>
      {pins.map(pin => (
        <Link
          key={pin.url}
          to={`/s/${session.id}?pin=${encodeURIComponent(pin.url)}&name=${encodeURIComponent(pin.name)}`}
          className={`block truncate rounded-md px-2 py-0.5 text-xs text-zinc-500 hover:bg-zinc-100 dark:hover:bg-zinc-900 ${nested ? 'ml-10' : 'ml-6'}`}
          title={pin.name}
        >
          📌 {pin.name}
        </Link>
      ))}
    </>
  )
}

export function Sidebar({
  bots,
  sessions,
  pins,
  onChange,
}: {
  bots: Bot[]
  sessions: Session[]
  pins: Record<string, { name: string; url: string }[]>
  onChange: () => void
}) {
  const navigate = useNavigate()
  const [showArchived, setShowArchived] = useState<Record<string, boolean>>({})

  async function openChat(bot: Bot, name: string) {
    const chat = await api.openChat(bot.name, name)
    onChange()
    navigate(`/s/${chat.id}`)
  }

  async function newThread(bot: Bot) {
    const title = window.prompt('Thread title', '')
    if (title === null) return
    const thread = await api.newThread(bot.name, title)
    onChange()
    navigate(`/s/${thread.id}`)
  }

  return (
    <nav className="flex h-full flex-col gap-4 overflow-y-auto p-3">
      <Link to="/" className="px-2 text-lg font-semibold">
        Ergonaut
      </Link>
      <Link to="/sessions" className="rounded-md px-2 py-1 text-sm hover:bg-zinc-100 dark:hover:bg-zinc-900">
        🔎 All sessions
      </Link>
      {bots.map(bot => {
        const mine = sessions.filter(s => s.bot === bot.name)
        const chats = bot.chats?.length
          ? bot.chats
          : [{ name: 'main', description: '', session_id: bot.root_session_id }]
        const threads = mine.filter(s => s.role === 'thread' && s.status !== 'completed')
        const archived = mine.filter(s => s.role === 'thread' && s.status === 'completed')
        return (
          <section key={bot.name}>
            <div className="mb-1 flex items-center px-2">
              <Link
                to={`/bots/${bot.name}`}
                title="Bot options: tools and skills"
                className="text-xs font-semibold tracking-wide text-zinc-500 uppercase hover:text-zinc-800 dark:hover:text-zinc-200"
              >
                {bot.name} ⚙
              </Link>
              {bot.parent && <span className="ml-1 text-[10px] text-zinc-400">in {bot.parent}</span>}
              {bot.orchestration && (
                <button
                  className="ml-auto rounded px-1.5 text-xs text-zinc-500 hover:bg-zinc-100 dark:hover:bg-zinc-900"
                  title="New thread"
                  onClick={() => newThread(bot)}
                >
                  + thread
                </button>
              )}
            </div>
            {chats.map(chat => {
              const session = mine.find(s => s.id === chat.session_id)
              return session ? (
                <div key={chat.name}>
                  <SessionLink session={session} />
                  <PinLinks session={session} pins={pins[session.id]} />
                </div>
              ) : (
                <button
                  key={chat.name}
                  title={chat.description || undefined}
                  className="w-full rounded-md px-2 py-1 text-left text-sm text-indigo-600 hover:bg-zinc-100 dark:hover:bg-zinc-900"
                  onClick={() => openChat(bot, chat.name)}
                >
                  {chat.name === 'main' ? 'Start chatting' : `Open ${chat.description || chat.name}`}
                </button>
              )
            })}
            {threads.map(t => (
              <div key={t.id}>
                <SessionLink session={t} nested />
                <PinLinks session={t} pins={pins[t.id]} nested />
              </div>
            ))}
            {!!archived.length && (
              <button
                className="ml-4 px-2 py-0.5 text-xs text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-300"
                onClick={() => setShowArchived(open => ({ ...open, [bot.name]: !open[bot.name] }))}
              >
                {showArchived[bot.name] ? '▾' : '▸'} {archived.length} archived
              </button>
            )}
            {showArchived[bot.name] && archived.map(t => <SessionLink key={t.id} session={t} nested />)}
            {bot.knowledge && (
              <NavLink
                to={`/bots/${bot.name}/kb`}
                className={({ isActive }) =>
                  `block rounded-md px-2 py-1 text-sm text-zinc-500 ${
                    isActive ? 'bg-zinc-200 font-medium dark:bg-zinc-800' : 'hover:bg-zinc-100 dark:hover:bg-zinc-900'
                  }`
                }
              >
                Memory
              </NavLink>
            )}
          </section>
        )
      })}
      {!bots.length && <p className="px-2 text-sm text-zinc-500">No bots are loaded. Set ERGONAUT_BOTS and restart.</p>}
      <Link to="/costs" className="mt-auto rounded-md px-2 py-1 text-sm hover:bg-zinc-100 dark:hover:bg-zinc-900">
        💲 Costs
      </Link>
    </nav>
  )
}
