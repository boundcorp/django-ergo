import { useRef, useState } from 'react'
import { api, type RecurringWake, type RecurringWakeAction } from '../api'

function ExactTime({ value }: { value: string }) {
  return <time dateTime={value}>{new Date(value).toISOString().replace('T', ' ').replace('.000Z', ' UTC')}</time>
}

function Schedule({
  sessionId,
  schedule,
  onChange,
}: {
  sessionId: string
  schedule: RecurringWake
  onChange: (schedule: RecurringWake) => void
}) {
  const [pending, setPending] = useState<RecurringWakeAction | null>(null)
  const [error, setError] = useState('')
  const submitting = useRef(false)
  const terminal = schedule.status === 'completed' || schedule.status === 'cancelled'
  async function control(action: RecurringWakeAction) {
    if (submitting.current) return
    submitting.current = true
    setPending(action)
    setError('')
    try {
      onChange(await api.controlRecurringWake(sessionId, schedule.id, action))
    } catch (e) {
      setError(`Could not ${action} recurring wake: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      submitting.current = false
      setPending(null)
    }
  }
  const interval =
    schedule.interval_seconds % 60 === 0 ? `${schedule.interval_seconds / 60} min` : `${schedule.interval_seconds} sec`
  return (
    <li className="rounded-card border border-line px-3 py-2">
      <p className="break-words font-medium text-ink">{schedule.note}</p>
      <p>
        <strong className="capitalize">{schedule.status}</strong> · Every {interval} · {schedule.occurrences}
        {schedule.max_occurrences != null ? ` / ${schedule.max_occurrences}` : ''} occurrences
      </p>
      {schedule.next_run_at && !terminal && (
        <p>
          Next run: <ExactTime value={schedule.next_run_at} />
        </p>
      )}
      {schedule.expires_at && (
        <p>
          Expires: <ExactTime value={schedule.expires_at} />
        </p>
      )}
      <p>
        {schedule.periodic_checkins
          ? 'Periodic check-ins: reports even when unchanged.'
          : 'Quiet: reports only when something changes.'}
      </p>
      {schedule.blocked_reason && <p className="text-ink">Blocked: {schedule.blocked_reason}</p>}
      {!terminal && (
        <div className="mt-1 flex gap-3">
          {(schedule.status === 'active' ? ['pause', 'cancel'] : ['resume', 'cancel']).map(action => (
            <button
              key={action}
              type="button"
              className="capitalize underline disabled:opacity-50"
              disabled={pending !== null}
              aria-label={`${action} recurring wake: ${schedule.note}`}
              onClick={() => control(action as RecurringWakeAction)}
            >
              {pending === action ? `${action}…` : action}
            </button>
          ))}
        </div>
      )}
      {error && (
        <p role="alert" className="mt-1 text-ink">
          {error}
        </p>
      )}
    </li>
  )
}

export function RecurringWakeNotice({
  sessionId,
  schedules,
  onChange,
}: {
  sessionId: string
  schedules: RecurringWake[]
  onChange: (schedule: RecurringWake) => void
}) {
  if (!schedules.length) return null
  return (
    <section aria-label="Recurring thread wakes" className="mx-4 mt-2 text-xs text-muted">
      <h2 className="mb-1 font-medium text-ink">Recurring thread wakes</h2>
      <ul tabIndex={0} aria-label="Recurring wake schedules" className="flex max-h-48 flex-col gap-1 overflow-y-auto">
        {schedules.map(schedule => (
          <Schedule key={schedule.id} sessionId={sessionId} schedule={schedule} onChange={onChange} />
        ))}
      </ul>
    </section>
  )
}
