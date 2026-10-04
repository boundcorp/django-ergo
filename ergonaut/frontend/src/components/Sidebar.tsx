import { useState } from 'react'
import { Link, NavLink, useMatch, useNavigate } from 'react-router-dom'
import type { Bot, Session, SidebarPin } from '../api'
import { api } from '../api'
import BotIcon from './BotIcon'
import { pinIcon } from './Pins'

// A chat's state: a spinner while it works, otherwise a dot (attention, unread, delegated, done, open).
function StatusDot({ session, unread }: { session: Session; unread: boolean }) {
  return session.busy ? (
    <span
      title="Working"
      className="h-2.5 w-2.5 shrink-0 animate-spin rounded-full border-[1.5px] border-indigo-500 border-t-transparent"
    />
  ) : (
    <span
      title={
        session.attention
          ? 'Needs your attention'
          : unread
            ? 'Unread reply'
            : session.open_in
              ? 'Working on a delegated request'
              : session.open_out
                ? 'Waiting on another thread'
                : undefined
      }
      className={`shrink-0 rounded-full ${
        session.attention
          ? 'h-2 w-2 bg-amber-400'
          : unread
            ? 'h-2 w-2 bg-sky-500'
            : session.open_in
              ? 'h-1.5 w-1.5 animate-pulse bg-indigo-400'
              : session.status === 'completed'
                ? 'h-1.5 w-1.5 bg-zinc-400'
                : 'h-1.5 w-1.5 bg-emerald-500'
      }`}
    />
  )
}

function SessionLink({ session, nested }: { session: Session; nested?: boolean }) {
  // The open chat is being read, so its reply is never shown as unread.
  const open = useMatch(`/s/${session.id}`)
  const unread = session.unread && !open
  return (
    <NavLink
      to={`/s/${session.id}`}
      className={({ isActive }) =>
        `flex items-center gap-2 truncate rounded-lg px-3 py-2 text-sm ${nested ? 'ml-3' : ''} ${
          isActive ? 'bg-indigo-tint font-medium text-ink' : 'hover:bg-raised'
        }`
      }
    >
      <StatusDot session={session} unread={!!unread} />
      <span className="truncate">{session.title}</span>
      {!!session.open_out && (
        <span className="ml-auto shrink-0 text-xs text-zinc-400" title="Waiting on other threads">
          ⏳{session.open_out > 1 ? session.open_out : ''}
        </span>
      )}
    </NavLink>
  )
}

// The bot's name, opening its main chat (and showing that chat's state), or starting it.
function BotTitle({ bot, main, onStart }: { bot: Bot; main?: Session; onStart: () => void }) {
  const open = useMatch(main ? `/s/${main.id}` : '/__none__')
  const unread = !!main?.unread && !open
  const body = (
    <>
      <BotIcon bot={bot} />
      <span className="truncate">{bot.name}</span>
      {main && <StatusDot session={main} unread={unread} />}
      {!!main?.open_out && (
        <span className="text-xs font-normal text-zinc-400" title="Waiting on other threads">
          ⏳{main.open_out > 1 ? main.open_out : ''}
        </span>
      )}
    </>
  )
  const style = 'flex min-w-0 flex-1 items-center gap-2 rounded-lg px-2 py-2 text-sm font-semibold text-ink'
  return main ? (
    <NavLink
      to={`/s/${main.id}`}
      title={bot.description || undefined}
      className={({ isActive }) => `${style} ${isActive ? 'bg-indigo-tint' : 'hover:bg-raised'}`}
    >
      {body}
    </NavLink>
  ) : (
    <button title="Start chatting" className={`${style} text-left hover:bg-raised`} onClick={onStart}>
      {body}
    </button>
  )
}

// A chat's pinned files under it; each opens in that chat's viewer.
function PinLinks({ session, pins, nested }: { session: Session; pins?: SidebarPin[]; nested?: boolean }) {
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
          {pinIcon(pin)} {pin.name}
        </Link>
      ))}
    </>
  )
}

export function Sidebar({
  bots,
  sessions,
  pins,
  botErrors = [],
  onChange,
}: {
  bots: Bot[]
  sessions: Session[]
  pins: Record<string, SidebarPin[]>
  botErrors?: { folder: string; name: string; error: string }[]
  onChange: () => void
}) {
  const navigate = useNavigate()
  const [showArchived, setShowArchived] = useState<Record<string, boolean>>({})

  async function openChat(bot: Bot, name: string) {
    const chat = await api.openChat(bot.name, name)
    onChange()
    navigate(`/s/${chat.id}`)
  }

  function newThread(bot: Bot) {
    navigate(`/bots/${bot.name}/new-thread`)
  }

  return (
    <nav className="flex h-full flex-col gap-5 overflow-y-auto p-4">
      <Link to="/" className="sidebar-brand px-2 py-3">
        ERGONAUT_
      </Link>
      {botErrors.map(e => (
        <div
          key={e.folder}
          title={`${e.folder}\n${e.error}`}
          className="rounded-md border border-red-300 bg-red-50 px-2 py-1 text-xs text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300"
        >
          ⚠ {e.name} didn't load: <span className="break-words">{e.error.slice(0, 160)}</span>
        </div>
      ))}
      <div className="px-2 text-xs font-semibold uppercase tracking-wide text-muted">Bots</div>
      {bots.map(bot => {
        const mine = sessions.filter(s => s.bot === bot.name)
        const chats = bot.chats?.length
          ? bot.chats
          : [{ name: 'main', description: '', session_id: bot.root_session_id }]
        const main = mine.find(s => s.id === chats.find(c => c.name === 'main')?.session_id)
        const threads = mine.filter(s => s.role === 'thread' && s.status !== 'completed')
        const archived = mine.filter(s => s.role === 'thread' && s.status === 'completed')
        return (
          <section key={bot.name} className="space-y-1">
            <div className="flex items-center gap-1">
              <BotTitle bot={bot} main={main} onStart={() => openChat(bot, 'main')} />
              <Link
                to={`/bots/${bot.name}`}
                title="Bot options: tools and skills"
                className="rounded px-1 text-sm text-zinc-500 hover:bg-raised"
              >
                ⚙
              </Link>
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
            {main && <PinLinks session={main} pins={pins[main.id]} />}
            {chats
              .filter(chat => chat.name !== 'main')
              .map(chat => {
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
                    Open {chat.description || chat.name}
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
      <div className="mt-auto flex flex-col gap-1 border-t border-stroke pt-4">
        <Link to="/sessions" className="rounded-lg px-3 py-2 text-sm hover:bg-raised">
          History
        </Link>
        <Link to="/costs" className="rounded-lg px-3 py-2 text-sm hover:bg-raised">
          Costs
        </Link>
      </div>
    </nav>
  )
}
