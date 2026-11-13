import type { RealtimeVoiceSession } from '../api'
import { Link } from 'react-router-dom'

/** One durable entry per voice session; generated speech proves neither playback nor work. */
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
        <strong>Stage (reported, unverified)</strong>
        <p className="mt-1 text-muted">{state?.current_step || 'No current step recorded.'}</p>
        {!!state?.decisions?.length && <p className="mt-1">Decisions: {state.decisions.join('; ')}</p>}
        {!!state?.constraints?.length && <p className="mt-1">Constraints: {state.constraints.join('; ')}</p>}
        {!!state?.completed_steps?.length && (
          <p className="mt-1">Reported completed steps: {state.completed_steps.join('; ')}</p>
        )}
      </div>
      <VoiceBackgroundWork session={session} />
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

/** Durable request records, never inferred from generated assistant speech. */
export function VoiceBackgroundWork({ session }: { session: RealtimeVoiceSession }) {
  const pending = session.state?.pending_delegation
  const delegations = session.delegations ?? []
  const tasks = [...delegations, ...(session.thread_updates ?? [])]
  const budget = session.request_budget
  return (
    <div className="mt-3" aria-label="Background work">
      <strong>Background work · separate from voice SDK states</strong>
      {budget && (
        <div aria-label="Voice request allowance" className="mt-1">
          {budget.limit === null ? (
            budget.collecting ? (
              <p>Collecting the current request; wait for submission before making another voice request.</p>
            ) : budget.inflight ? (
              <p>Background work is pending. You can request another task without waiting for its result.</p>
            ) : (
              <p>Request background work when you need it; nothing starts automatically.</p>
            )
          ) : (
            <>
              <p>
                Voice requests: {budget.remaining} of {budget.limit} remaining · {budget.used} used.
              </p>
              {budget.collecting ? (
                <p>Collecting the current request; wait for submission before making another voice request.</p>
              ) : budget.inflight ? (
                <p>
                  A request is pending; wait for its result before making another voice request. Requests are sequential.
                </p>
              ) : (
                budget.remaining !== null &&
                budget.remaining > 0 && <p>Requests are sequential; nothing starts automatically.</p>
              )}
            </>
          )}
          {budget.remaining === 0 && (
            <p>
              Voice request allowance exhausted.{' '}
              <Link className="underline" to={`/s/${session.chat_id}`}>
                Continue in owning text thread
              </Link>
              .
            </p>
          )}
        </div>
      )}
      {delegations.length === 0 && (
        <p className="mt-1 text-muted">
          {pending?.status === 'collecting'
            ? 'Collecting the spoken request; nothing submitted yet.'
            : pending?.status === 'expired' || pending?.status === 'cancelled'
              ? `Request ${pending.status}; nothing submitted.`
              : 'No background request submitted; no verified history lookup result.'}
        </p>
      )}
      {tasks.map(task => (
        <div key={task.update_id ?? task.event_id} className="mt-2 rounded-control border border-stroke p-2">
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
                  : task.status === 'delivered'
                    ? 'Accepted by worker — execution not confirmed; no verified result yet.'
                    : task.status === 'waiting'
                      ? 'Waiting for normal thread approval — no verified result yet.'
                      : `Status: ${task.status || 'unknown'} — no verified result recorded.`}
          </div>
          {task.status === 'answered' && !task.error && task.result && (
            <p className="whitespace-pre-wrap" aria-label="Durable background result">
              {task.result}
            </p>
          )}
          {task.status === 'waiting' && (
            <Link className="underline" to={`/s/${task.chat_id ?? session.chat_id}`}>
              Review normal thread approval
            </Link>
          )}
          {task.error && <p className="whitespace-pre-wrap text-danger">{task.error}</p>}
        </div>
      ))}
    </div>
  )
}
