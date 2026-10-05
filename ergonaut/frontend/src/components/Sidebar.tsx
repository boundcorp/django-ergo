import { Link, NavLink, useMatch, useNavigate } from 'react-router-dom'
import type { Bot, Session, SidebarPin } from '../api'
import { api } from '../api'
import BotIcon from './BotIcon'
import { pinIcon } from './Pins'
import { StatusDot, ThreadGroups } from './ThreadList'
import VersionFooter from './VersionFooter'

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
      {session.resolved_summary ? (
        <span
          className="flex min-w-0 flex-col"
          title={`Resolved${session.resolved_by ? ` by ${session.resolved_by}` : ''}: ${session.resolved_summary}`}
        >
          <span className="truncate">{session.title}</span>
          <span className="truncate text-xs text-muted">{session.resolved_summary}</span>
        </span>
      ) : (
        <span className="truncate">{session.title}</span>
      )}
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

  async function openChat(bot: Bot, name: string) {
    const chat = await api.openChat(bot.name, name)
    onChange()
    navigate(`/s/${chat.id}`)
  }

  const waitingAll = sessions.filter(s => s.bucket === 'waiting' && s.status !== 'completed').length

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
      <NavLink
        to="/threads"
        end
        className={({ isActive }) =>
          `flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-semibold ${isActive ? 'bg-indigo-tint' : 'hover:bg-raised'}`
        }
      >
        Threads
        {!!waitingAll && (
          <span className="rounded-full bg-accent px-1.5 text-xs font-semibold text-white" title="Waiting on you">
            {waitingAll}
          </span>
        )}
        <span className="ml-auto text-xs font-normal text-muted">⤢</span>
      </NavLink>
      <div className="px-2 text-xs font-semibold uppercase tracking-wide text-muted">Bots</div>
      {bots.map(bot => {
        const mine = sessions.filter(s => s.bot === bot.name)
        const chats = bot.chats?.length
          ? bot.chats
          : [{ name: 'main', description: '', session_id: bot.root_session_id }]
        const main = mine.find(s => s.id === chats.find(c => c.name === 'main')?.session_id)
        const threads = mine.filter(s => s.role === 'thread')
        const waiting = threads.filter(s => s.bucket === 'waiting').length
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
              {!!threads.length && (
                <Link
                  to={`/threads?bot=${encodeURIComponent(bot.name)}`}
                  title="Open all of this bot's threads"
                  className="ml-auto rounded px-1.5 text-xs text-zinc-500 hover:bg-zinc-100 dark:hover:bg-zinc-900"
                >
                  {waiting ? <span className="text-warning">{waiting} waiting</span> : null} ⤢
                </Link>
              )}
              {bot.orchestration && (
                <button
                  className={`${threads.length ? '' : 'ml-auto'} rounded px-1.5 text-xs text-zinc-500 hover:bg-zinc-100 dark:hover:bg-zinc-900`}
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
            {!!threads.length && (
              <ThreadGroups
                sessions={threads}
                scope={`sidebar.${bot.name}`}
                after={t => <PinLinks session={t} pins={pins[t.id]} nested />}
              />
            )}
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
        <Link to="/routing" className="rounded-lg px-3 py-2 text-sm hover:bg-raised">
          Routing
        </Link>
        <Link to="/api-keys" className="rounded-lg px-3 py-2 text-sm hover:bg-raised">
          API keys
        </Link>
        <VersionFooter />
      </div>
    </nav>
  )
}
