// A <input type="datetime-local"> value is local wall-clock time with no zone. The server
// stores UTC. Convert both ways explicitly so saving a form never shifts a time by the
// browser's UTC offset.

/** An ISO instant (UTC or with an offset) as a datetime-local value in this browser's zone. */
export function toLocalInput(iso: string | null | undefined): string {
  if (!iso) return ''
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}` +
    `T${pad(at.getHours())}:${pad(at.getMinutes())}`
  )
}

/** A datetime-local value (this browser's wall clock) as a UTC ISO instant, or '' when empty. */
export function fromLocalInput(value: string): string {
  if (!value) return ''
  // The Date constructor reads a zone-less date-time as local time.
  const at = new Date(value)
  return Number.isNaN(at.getTime()) ? '' : at.toISOString()
}
