import type { Bot } from '../api'

// bot.yaml `color` names (django_ergo.bots.definition.COLORS), as Tailwind's 500 shades.
const PALETTE: Record<string, string> = {
  slate: '#64748b',
  red: '#ef4444',
  orange: '#f97316',
  amber: '#f59e0b',
  yellow: '#eab308',
  lime: '#84cc16',
  green: '#22c55e',
  emerald: '#10b981',
  teal: '#14b8a6',
  cyan: '#06b6d4',
  sky: '#0ea5e9',
  blue: '#3b82f6',
  indigo: '#6366f1',
  violet: '#8b5cf6',
  purple: '#a855f7',
  fuchsia: '#d946ef',
  pink: '#ec4899',
  rose: '#f43f5e',
}

/** The bot's color: its own, or one picked from its name so it stays the same. */
export function botColor(bot: Pick<Bot, 'name' | 'color'>): string {
  if (bot.color) return PALETTE[bot.color] ?? bot.color
  const names = Object.keys(PALETTE).filter(n => n !== 'slate')
  let hash = 0
  for (const ch of bot.name) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0
  return PALETTE[names[hash % names.length]]
}

/** A rounded badge in the bot's color with its icon (or first letter). */
export default function BotIcon({ bot, size = 22 }: { bot: Pick<Bot, 'name' | 'icon' | 'color'>; size?: number }) {
  const color = botColor(bot)
  const icon = bot.icon || bot.name.charAt(0).toUpperCase()
  return (
    <span
      aria-hidden
      className="inline-flex shrink-0 items-center justify-center rounded-md font-semibold leading-none"
      style={{
        width: size,
        height: size,
        fontSize: size * (bot.icon ? 0.62 : 0.55),
        color,
        background: `color-mix(in srgb, ${color} 22%, transparent)`,
        boxShadow: `inset 0 0 0 1px color-mix(in srgb, ${color} 45%, transparent)`,
      }}
    >
      {icon}
    </span>
  )
}
