import type { Approval } from '../api'

/**
 * Tool calls a turn is waiting on, docked above the composer (not in the transcript: it must stay in view while the
 * reader is scrolled away, and the composer is disabled until it is answered). Decisions are collected and sent
 * together by the page (see `chooseApproval` in chatAdapter.ts).
 */
export function ApprovalPanel({
  waiting,
  choices,
  disabled,
  onDecide,
  onRevalidate,
}: {
  waiting: Approval[]
  choices: Record<string, boolean>
  disabled: boolean
  onDecide: (approvalId: string, approve: boolean) => void
  onRevalidate: () => void
}) {
  if (!waiting.length) return null
  return (
    <div className="mx-4 mb-3 grid max-h-[45dvh] gap-2 overflow-y-auto sm:mx-6">
      <button
        disabled={disabled}
        className="rounded-control border border-warning/50 px-3 py-2 text-sm text-ink disabled:opacity-50"
        onClick={onRevalidate}
      >
        Re-check permissions and run if authorized
      </button>
      {waiting.map(approval => (
        <section key={approval.id} className="rounded-card border border-warning/40 bg-amber-tint p-4 text-sm">
          <div className="font-medium">Tool approval requested: {approval.name}</div>
          <p className="mt-1 text-muted">Review this tool call; the composer waits for its decision.</p>
          <pre className="mt-3 max-h-56 overflow-auto whitespace-pre-wrap rounded-control border border-warning/30 bg-canvas p-3 text-xs text-ink">
            {JSON.stringify(approval.input, null, 2)}
          </pre>
          {approval.preview && (
            <pre className="mt-3 max-h-56 overflow-auto whitespace-pre-wrap rounded-control border border-warning/30 bg-canvas p-3 text-xs text-ink">
              {approval.preview}
            </pre>
          )}
          <div className="mt-3 flex items-center gap-2">
            <button
              disabled={disabled}
              className={`rounded-control px-4 py-2 font-semibold disabled:opacity-50 ${
                choices[approval.id] === false ? 'border border-warning/50 text-muted' : 'bg-warning text-canvas'
              }`}
              onClick={() => onDecide(approval.id, true)}
            >
              {choices[approval.id] === true ? 'Approved' : 'Approve'}
            </button>
            <button
              disabled={disabled}
              className={`rounded-control border px-4 py-2 disabled:opacity-50 ${
                choices[approval.id] === false ? 'border-danger/60 text-danger' : 'border-warning/50'
              }`}
              onClick={() => onDecide(approval.id, false)}
            >
              {choices[approval.id] === false ? 'Rejected' : 'Reject'}
            </button>
            {approval.id in choices && waiting.length > 1 && (
              <span className="text-xs text-muted">Sent once every call has an answer</span>
            )}
          </div>
        </section>
      ))}
    </div>
  )
}
