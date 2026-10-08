import type { Session } from './api'

// The top bar's location: crumbs leading to the page, then the page's own title.
export type Crumb = { label: string; to?: string }
export type Location = { crumbs: Crumb[]; title: string }

const PAGES: Record<string, string> = {
  '/threads': 'Threads',
  '/sessions': 'History',
  '/costs': 'Costs & usage',
  '/routing': 'Routing',
  '/api-keys': 'API keys',
}

export function topbarLocation(pathname: string, sessions: Session[] = []): Location {
  const path = pathname.replace(/\/+$/, '') || '/'
  if (path === '/') return { crumbs: [], title: 'Workspace' }
  if (PAGES[path]) return { crumbs: [{ label: 'Workspace', to: '/' }], title: PAGES[path] }
  const chat = /^\/s\/([^/]+)/.exec(path)
  if (chat) {
    const session = sessions.find(s => s.id === chat[1])
    if (!session) return { crumbs: [{ label: 'Workspace', to: '/' }], title: 'Conversation' }
    const crumbs: Crumb[] = [{ label: session.bot, to: `/bots/${session.bot}` }]
    const parent = session.parent_id && sessions.find(s => s.id === session.parent_id)
    if (parent && parent.title) crumbs.push({ label: parent.title, to: `/s/${parent.id}` })
    return { crumbs, title: session.title || 'Main chat' }
  }
  const bot = /^\/bots\/([^/]+)(\/.*)?$/.exec(path)
  if (bot) {
    const name = decodeURIComponent(bot[1])
    if (bot[2] === '/new-thread') return { crumbs: [{ label: name, to: `/bots/${bot[1]}` }], title: 'New thread' }
    if (bot[2] === '/kb') return { crumbs: [{ label: name, to: `/bots/${bot[1]}` }], title: 'Memory' }
    return { crumbs: [{ label: 'Bots' }], title: name }
  }
  return { crumbs: [], title: 'Workspace' }
}
