import type { RealtimeVoiceSession } from '../api'

/** One durable entry per voice session; generated speech proves neither playback nor work. */
export function VoiceSessionCard({ session }: { session: RealtimeVoiceSession }) {
  const state = session.state
  const delegations = session.delegations ?? []
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
        <strong>Stage (reported, unverified)</strong>
        <p className="mt-1 text-muted">{state?.current_step || 'No current step recorded.'}</p>
        {!!state?.decisions?.length && <p className="mt-1">Decisions: {state.decisions.join('; ')}</p>}
        {!!state?.constraints?.length && <p className="mt-1">Constraints: {state.constraints.join('; ')}</p>}
        {!!state?.completed_steps?.length && (
          <p className="mt-1">Reported completed steps: {state.completed_steps.join('; ')}</p>
        )}
      </div>
      <div className="mt-3" aria-label="Background work">
        <strong>Background work</strong>
        {delegations.length === 0 && (
          <p className="mt-1 text-muted">No background request submitted; no verified history lookup result.</p>
        )}
        {delegations.map(task => (
          <div key={task.event_id} className="mt-2 rounded-control border border-stroke p-2">
            <div>{task.text}</div>
            <div className="text-muted">
              {task.error || task.status === 'failed' || task.status === 'error'
                ? 'Failed — no verified result.'
                : task.status === 'answered'
                  ? task.result
                    ? 'Answered — durable background result recorded.'
                    : 'Answered — no text result recorded.'
                  : task.status === 'queued'
                    ? 'Queued — may not have started; no verified result.'
                    : `Status: ${task.status || 'unknown'} — no verified result recorded.`}
            </div>
            {task.status === 'answered' && !task.error && task.result && (
              <p className="whitespace-pre-wrap" aria-label="Durable background result">
                {task.result}
              </p>
            )}
            {task.error && <p className="whitespace-pre-wrap text-danger">{task.error}</p>}
          </div>
        ))}
      </div>
      <details className="mt-3">
        <summary className="cursor-pointer text-muted">Transcript ({session.events.length})</summary>
        <p className="mt-2 text-muted">Assistant speech is generated and unverified as work evidence.</p>
        <div className="mt-2 max-h-64 overflow-y-auto">
          {session.events.map(event => (
            <p key={event.event_id} className="mb-2 whitespace-pre-wrap">
              <strong>{event.kind === 'user' ? 'You' : event.kind === 'assistant' ? 'Assistant' : event.kind}:</strong>{' '}
              {event.text}
              {event.kind === 'assistant' && (
                <span className="text-muted">
                  {' '}
                  {event.delivery === 'interrupted'
                    ? '(interrupted)'
                    : event.delivery === 'delivered'
                      ? '(playback confirmed; hearing not established)'
                      : '(playback unconfirmed)'}
                </span>
              )}
            </p>
          ))}
        </div>
      </details>
    </section>
  )
}
