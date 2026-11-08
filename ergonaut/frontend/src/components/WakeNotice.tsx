import type { Wake } from '../api'
import { clock } from '../time'

/** "in 12 min", "in 3 h", "now" until a wake is due. */
export function untilDue(iso: string, now = Date.now()): string {
  const minutes = Math.round((new Date(iso).getTime() - now) / 60000)
  if (minutes < 1) return 'now'
  if (minutes < 60) return `in ${minutes} min`
  return `in ${Math.round(minutes / 60)} h`
}

// The bot's pending self-wake (django_ergo.bots.wakes): it will continue on its own then.
// Shown above the composer while one is pending.
export function WakeNotice({ wakes, now = Date.now() }: { wakes: Wake[]; now?: number }) {
  if (!wakes.length) return null
  return (
    <section aria-label="Pending self-wake" className="wake-notice mx-4 mt-2 flex flex-col gap-1 text-xs text-muted">
      {wakes.map(wake => (
        <p key={wake.id} className="min-w-0 truncate" title={wake.note}>
          <span className="font-medium text-ink">
            ⏰ Continues on its own at {clock(wake.due_at)} ({untilDue(wake.due_at, now)})
          </span>{' '}
          · {wake.note}
        </p>
      ))}
    </section>
  )
}
