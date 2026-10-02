import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import type { BotDetail } from '../api'
import { api } from '../api'
import Changes from '../components/Changes'

function Section({ title, count, children }: { title: string; count?: number; children: React.ReactNode }) {
  return (
    <section className="mt-6">
      <h2 className="mb-2 text-sm font-semibold tracking-wide text-zinc-500 uppercase">
        {title}
        {count != null && <span className="ml-2 font-normal">{count}</span>}
      </h2>
      {children}
    </section>
  )
}

export function BotPage() {
  const { name = '' } = useParams()
  const [bot, setBot] = useState<BotDetail | null>(null)
  const [error, setError] = useState('')
  const [open, setOpen] = useState<string | null>(null)

  useEffect(() => {
    setBot(null)
    api
      .bot(name)
      .then(setBot)
      .catch(e => setError(String(e.message ?? e)))
  }, [name])

  if (!bot) return <div className="p-6 text-zinc-500">{error || 'Loading…'}</div>

  const facts = [
    ['Engine', [bot.engine, bot.model].filter(Boolean).join(' · ') || 'default'],
    ['Threads', bot.orchestration ? 'On: the chat can start and run threads' : 'Off'],
    ['Timezone', bot.timezone || 'server default'],
    ['Plugins', bot.plugins.join(', ') || 'none'],
    ['Folder', bot.folder],
  ]

  return (
    <div className="h-full overflow-y-auto px-6 py-6">
      <h1 className="text-xl font-semibold">{bot.name}</h1>
      {bot.description && <p className="mt-1 text-zinc-600 dark:text-zinc-400">{bot.description}</p>}
      <Link to={`/bots/${bot.name}/kb`} className="mt-2 inline-block text-sm text-indigo-600 hover:underline">
        Browse knowledge →
      </Link>

      <dl className="mt-4 grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm">
        {facts.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-zinc-500">{label}</dt>
            <dd className="font-mono text-xs leading-5 break-all">{value}</dd>
          </div>
        ))}
      </dl>

      {bot.manages_repo && (
        <Section title="Changes">
          <Changes bot={bot.name} />
        </Section>
      )}

      {!!bot.schedules?.length && (
        <Section title="Schedules" count={bot.schedules.length}>
          <table className="w-full text-sm">
            <tbody>
              {bot.schedules.map(s => (
                <tr key={s.name} className="border-t border-zinc-200 align-top dark:border-zinc-800">
                  <td className="py-1.5 pr-4 font-mono text-xs whitespace-nowrap">{s.name}</td>
                  <td className="py-1.5 pr-4 font-mono text-xs whitespace-nowrap text-zinc-500">{s.cron}</td>
                  <td className="py-1.5 pr-4">
                    {s.message}
                    <div className="text-xs text-zinc-500">
                      {s.to === 'thread'
                        ? `in a new thread “${s.thread_title}”`
                        : s.to === 'main'
                          ? 'in the main chat'
                          : `in ${s.to}`}
                      {s.users.length ? ` · for ${s.users.join(', ')}` : ''}
                    </div>
                  </td>
                  <td className="py-1.5 text-xs whitespace-nowrap text-zinc-500">
                    {!s.enabled ? 'off' : s.next_run ? `next ${new Date(s.next_run).toLocaleString()}` : 'not soon'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      )}

      <Section title="Skills" count={bot.skills.length}>
        <p className="mb-2 text-xs text-zinc-500">
          Chats load a skill when they need it and get its tools; skills marked “always” are loaded from the start
          there.
        </p>
        <ul className="divide-y divide-zinc-200 rounded-lg border border-zinc-200 dark:divide-zinc-800 dark:border-zinc-800">
          {bot.skills.map(skill => (
            <li key={skill.name}>
              <button
                className="flex w-full flex-wrap items-baseline gap-x-3 px-3 py-2 text-left hover:bg-zinc-50 dark:hover:bg-zinc-900"
                onClick={() => setOpen(open === skill.name ? null : skill.name)}
              >
                <span className="font-mono text-sm">{skill.name}</span>
                <span className="text-sm text-zinc-500">{skill.description}</span>
                <span className="ml-auto text-xs text-zinc-400">
                  {skill.tools?.length ? `${skill.tools.length} tools` : 'instructions'}
                  {skill.always_in?.length ? ` · always in ${skill.always_in.join(', ')}` : ''}
                </span>
              </button>
              {open === skill.name && (
                <div className="mx-3 mb-3 flex flex-col gap-2">
                  {skill.source && <div className="text-xs text-zinc-500">from {skill.source}</div>}
                  {skill.body && (
                    <pre className="overflow-auto rounded bg-zinc-50 p-3 text-xs whitespace-pre-wrap dark:bg-zinc-900">
                      {skill.body}
                    </pre>
                  )}
                  {!!skill.tools?.length && (
                    <table className="w-full text-sm">
                      <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
                        {skill.tools.map(tool => (
                          <tr key={tool.name} className="align-top">
                            <td className="py-1.5 pr-4 font-mono text-xs whitespace-nowrap">
                              {tool.name}
                              {tool.requires_approval && (
                                <span className="ml-2 rounded bg-amber-100 px-1 text-[10px] text-amber-800 dark:bg-amber-900/40 dark:text-amber-300">
                                  approval
                                </span>
                              )}
                            </td>
                            <td className="py-1.5 text-zinc-600 dark:text-zinc-400">{tool.description}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              )}
            </li>
          ))}
        </ul>
      </Section>

      <Section title="Instructions">
        <pre className="overflow-auto rounded-lg bg-zinc-50 p-3 text-xs whitespace-pre-wrap dark:bg-zinc-900">
          {bot.instructions || '(none)'}
        </pre>
      </Section>
    </div>
  )
}
