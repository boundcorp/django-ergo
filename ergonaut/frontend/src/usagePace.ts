const HOUR = 3600
const DAY = 24 * HOUR

type WindowWithReset = { resets_at: number | null }

/** The duration represented by a provider window key, when Ergo can infer it. */
export function windowSeconds(key: string): number | null {
  if (key === 'five_hour') return 5 * HOUR
  if (key === 'weekly' || key.startsWith('weekly_')) return 7 * DAY
  const minutes = /^minutes_(\d+)$/.exec(key)
  return minutes ? Number(minutes[1]) * 60 : null
}

/** Percent of a window that has elapsed, or null when its pace cannot be known. */
export function pace(key: string, window: WindowWithReset, now = Date.now() / 1000): number | null {
  const length = windowSeconds(key)
  if (!length || !window.resets_at) return null
  const left = window.resets_at - now
  if (left <= 0) return null
  return Math.max(0, Math.min(100, ((length - left) / length) * 100))
}
