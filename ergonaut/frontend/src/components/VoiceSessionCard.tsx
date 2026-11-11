import type { RealtimeVoiceSession } from '../api'

/** One durable entry per voice session; generated speech is not proof of playback. */
export function VoiceSessionCard({ session }: { session: RealtimeVoiceSession }) {
  const state = session.state
  return (
    <section className="mt-6 rounded-card border border-stroke bg-surface p-4 text-sm" aria-label="Voice session">
      <div className="flex flex-wrap items-center gap-2">
        <strong>Voice session</strong>
        <span className="text-muted">{session.status === 'active' ? 'Active' : 'Ended'}</span>
        {session.created_at && (
          <time className="text-muted" dateTime={session.created_at}>
            {new Date(session.created_at).toLocaleString()}
          </time>
        )}
      </div>
      <div className="mt-3" aria-label="Stage">
        <strong>Stage</strong>
        <p className="mt-1 text-muted">{state?.current_step || 'No current step recorded.'}</p>
        {!!state?.decisions?.length && <p className="mt-1">Decisions: {state.decisions.join('; ')}</p>}
        {!!state?.constraints?.length && <p className="mt-1">Constraints: {state.constraints.join('; ')}</p>}
        {(session.delegations ?? []).map(task => (
          <div key={task.event_id} className="mt-2 rounded-control border border-stroke p-2">
            <div>{task.text}</div>
            <div className="text-muted">{task.status}</div>
            {task.result && <p className="whitespace-pre-wrap">{task.result}</p>}
            {task.error && <p className="whitespace-pre-wrap text-danger">{task.error}</p>}
          </div>
        ))}
      </div>
      <details className="mt-3">
        <summary className="cursor-pointer text-muted">Transcript ({session.events.length})</summary>
        <div className="mt-2 max-h-64 overflow-y-auto">
          {session.events.map(event => (
            <p key={event.event_id} className="mb-2 whitespace-pre-wrap">
              <strong>{event.kind === 'user' ? 'You' : event.kind === 'assistant' ? 'Assistant' : event.kind}:</strong>{' '}
              {event.text}
              {event.kind === 'assistant' && (
                <span className="text-muted">
                  {' '}
                  ({event.delivery === 'interrupted' ? 'interrupted' : 'playback unconfirmed'})
                </span>
              )}
            </p>
          ))}
        </div>
      </details>
    </section>
  )
}
