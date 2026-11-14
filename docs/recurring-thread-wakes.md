# Recurring thread wakes

Canonical epic: [#334](https://github.com/boundcorp/django-ergo/issues/334).

## Usage and controls

Ask the bot: “check this chat every 5 minutes for the next 10 hours.”
The built-in wake skill provides:

- `ergo_wake_recurring(note, interval_minutes, duration_minutes | expires_at,
  max_occurrences?, periodic_checkins=false)`. Either a duration/expiry or a
  positive maximum count is mandatory; both limits may apply, whichever ends
  first. The first check runs after one interval, never immediately.
- `ergo_wake_recurring_list()` shows this chat's schedules and finite limits.
- `ergo_wake_recurring_control(schedule_id, action)` pauses, resumes or cancels.
  Resume does not extend the original expiry or replenish occurrence count.

Expiry is exclusive: no new turn starts at or after it. Count measures
dispatched occurrences, including failed or cancelled queued work, not successful
answers. Missed/coalesced slots do not consume count. A recurring turn cannot
create another recurring schedule or a one-shot wake to renew its lifetime.
Cancel does not interrupt an already running turn; use the ordinary turn-stop
control for that boundary.
Archive cancels schedules permanently; reopening the chat does not reactivate
them. Deleting a chat cascades its schedules and occurrence records.

The chat displays next run, expiry/count, interval, status and blocking reason.
An active schedule can temporarily show `thread_busy`, `awaiting_approval` or
`previous_occurrence_outstanding`; checks coalesce until the thread becomes free.
Intervals are best-effort eligibility, not a real-time guarantee. The minimum
interval also bounds aggregate recurring dispatches within one thread; competing
schedules rotate fairly rather than starting overlapping turns.
A final dispatch/delivery/turn failure changes the schedule to `blocked` and
requires explicit resume. Approval itself remains in the existing approval UI;
resume is not approval, and never bypasses tool or execution policy.

Unchanged quiet checks use an explicit `unchanged` structured result and produce
no final all-clear chat message. Changed results, questions and attachments stay
visible. `periodic_checkins=true` is only for a user who requests periodic
updates even without a change. Quiet suppresses routine notifications, **not**
model costs; every delivered check is an ordinary turn under existing policy.
No financial cap is inferred from finite scheduling limits.

Library settings, under `DJANGO_ERGO`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `RECURRING_WAKE_MIN_INTERVAL_SECONDS` | 60 | Minimum interval per schedule |
| `RECURRING_WAKE_MAX_PER_THREAD` | 3 | Live schedules per thread |
| `RECURRING_WAKE_MAX_GLOBAL` | 100 | Live schedules across the database |

Active, paused and blocked schedules reserve quota. These are library defaults,
not deployment infrastructure changes. A host without Ergonaut must invoke
`django_ergo.bots.recurring_wakes.fire_due()` on its bounded scheduler and keep
ordinary thread-message delivery/recovery configured.

The owner-scoped API supports `GET /api/sessions/{id}/recurring-wakes` and
`POST /api/sessions/{id}/recurring-wakes/{schedule_id}` with
`{"action": "pause" | "resume" | "cancel"}`. Existing bot access checks apply;
unknown schedules return 404 and invalid transitions return 409. Session JSON and
SSE include `recurring_wakes` so controls do not require a new chat message.

## Durability and timing

`RecurringWake` is the durable schedule; `RecurringWakeOccurrence` is a unique
schedule/elapsed-time slot with its own UUID and ordinary thread-message link.
`RecurringWakeLock` serializes quota admission, including first creation.
Session then schedule row locks serialize ticks, controls and occurrence claims.
Occurrence/message creation commits atomically. Existing message redispatch
recovers a lost dispatch without creating another tick. Busy threads and missed
slots produce at most one current check, never a replay of every missed slot.

Finite authority follows delegated requests, replies and worker updates/results
through a separate `recurring_origin` restriction. `Worker.recurring_origin` is
a nullable UUID without a foreign key, so deleting a schedule cannot erase the
restriction on its outstanding work. Descendant turns cannot create recurring or
one-shot wakes to renew that authority. This marker is **not** an occurrence:
ordinary delegated/worker results keep their natural notification and lifecycle
behavior, rather than acquiring quiet-check semantics.

After a worker crash, an abandoned claimed occurrence has an **unknown outcome**,
not an assumed safe-to-retry outcome. Recovery checks the matching call using the
existing running-turn and actionable-approval rules, with a short claim/handoff
grace period. Stale or superseded work becomes failed and blocks its schedule;
explicit resume admits a future slot without replaying uncertain work or
replenishing its count. Missing heartbeat evidence never means the thread is idle.

`scheduled_for` is the original selected slot and `dispatched_at` is the observed
scheduler enqueue time; neither proves a recipient claimed or consumed the work.
Opt-in lifecycle observations distinguish schedule admission from observed
occurrence request/queue time and correlate schedule, occurrence and message IDs.
Intended due times remain in occurrence fields, not fabricated past ledger events.
Existing message dispatch/claim, initial input, first successful
engine response and persisted-result hooks remain distinct. A successful engine
response is not semantic acceptance. Missing observations stay unknown and no
runtime collection flag is enabled here.

## Initial inspection and implementation plan

Baseline: `7d889a289dcc16cfdf9ef520b5a91edb11d79bd3` on a dedicated worktree.
Existing `bots/wakes.py` implements bounded one-shot self-wakes and delegates to
`messaging.send_event`; `ergonaut.fire_wakes` runs every 30 seconds. Thread-message
queueing, owner permissions, turn locks, approvals and delivery recovery already
exist; recurring wakes reuse them rather than adding a model runner or transport.
Worker/task results and PR-check notifications already arrive naturally. Recurring
checks complement those events; they are not a replacement or retry mechanism.

Collision review: PR #330 is passive cost integration, not spend authorization or
budget enforcement. PR #333 restricts generic table writes, not trusted scheduler
ORM writes. This change does not modify either boundary. Lifecycle PRs #318/#327
(epic #317) provide opt-in idempotent observations and existing message/turn hooks.
No runtime flag activation is included.

Implementation decisions:

- Add durable schedule and occurrence identities with an additive migration;
  transactionally persist each occurrence and its ordinary thread message.
- Require an expiry or maximum occurrence count. Use elapsed UTC intervals, not
  wall-clock cron: explicit-offset ISO timestamps for expiry; durations remain
  unchanged across daylight-saving transitions.
- Admit under a database lock with configurable minimum interval, per-thread and
  global quotas. Paused and blocked schedules retain quota reservations.
- Coalesce busy threads and overdue slots; at most one new occurrence per schedule
  per sweep and no backlog storm. Original scheduled time and actual dispatch time
  remain distinct. No scheduler model/provider calls.
- Pause/resume/cancel via chat tools and owner-authorized UI; expose next run,
  expiry, count and blocking reason. Archive cancels; deletion cascades.
- Reuse delivery policy and approval gates. Failed/budget-blocked occurrences do
  not automatically retry or obtain fresh authority. Explicit resume is a control
  operation, not an approval or a spend-cap override.
- Quiet unchanged checks by default, using an explicit unchanged result; questions,
  attachments and changed results remain visible. Periodic check-ins are opt-in.
- Prove behavior with isolated PostgreSQL, fake model responses and a finite,
  model-free smoke scenario. No production restart, deploy, polling or paid call.

## Acceptance matrix

| Requirement | Implementation boundary | Evidence required |
| --- | --- | --- |
| Finite lifetime | expiry/max-occurrence admission and delivery checks | expiry/count boundary tests |
| Finite authority through follow-ups | separate inherited origin restriction | delegated/reply/worker renewal-denial tests |
| Rate and quotas | configured interval and serialized quota admission | invalid interval and concurrent quota tests |
| Identity and idempotency | schedule/occurrence UUIDs and locked tick transaction | duplicate/concurrent scheduler tests |
| Busy/approval coalescing | existing busy rule and outstanding message gate | busy/approval tests |
| Recovery and clock jumps | anchored slots, bounded coalescing, existing redispatch | forward/backward jump and replay tests |
| Pause/resume/cancel | serialized control and delivery claims | transition and queued cancellation tests |
| Archive/delete | explicit cancellation and FK cascades | archive/delete tests |
| Policy and budgets | existing normal delivery gates; persistent failure block | budget denial/no automatic retry tests |
| Quiet notifications | recurring-only unchanged response contract | unchanged/changed/check-in fake-engine tests |
| Lifecycle timing | requested/queued occurrence correlation plus existing message hooks | opt-in ledger correlation tests |
| User controls | owner-scoped API, SSE and visible chat controls | API authorization and rendered UI smoke |
| Compatibility | additive schema; old one-shot behavior unchanged | migration drift and one-shot regression suite |

## Review and bounded verification

Independent core and application reviews found and closed: abandoned delivery
and superseded-approval leaks, claim/result-handoff grace races, one-shot and
delegated renewal escapes, non-UTC timing, per-thread/global-batch starvation,
Telegram notification omission and unbounded retained UI history. Targeted source
re-reviews reported no remaining actionable blockers; execution is a separate gate.

- PostgreSQL tests use isolated temporary pgserver instances, with test-only
  `fsync`/synchronous-commit durability settings disabled. They prove real row-lock
  concurrency, not production storage durability or failover behavior.
- Django 5.2.13 on Python 3.11: 160 focused scheduler/delivery/one-shot/budget/
  lifecycle/import-layer/natural-worker/Telegram regressions passed.
- Django 4.2.30 on Python 3.11: 99 focused wake/delivery/one-shot tests passed;
  migration drift check passed. Django 5.2.13 is the primary local test runtime.
- Ergonaut owner/API/stream regressions: 69 passed. The isolated suite emitted
  default-secret/static-directory warnings and one database-teardown warning
  about another open session; no test failed.
- All frontend tests: 356 passed; TypeScript/Vite production build passed.
  Existing React `act` warnings and the large-bundle advisory remain.
- Actual controlled-clock, model-free PostgreSQL smoke: two occurrences completed;
  duplicate ticks were no-ops and duplicate claims rejected. A third interrupted
  claim became blocked with an unknown outcome, was not replayed, and cancellation
  prevented future ticks. Zero provider calls.
- Chromium rendered the actual controls in an isolated fixture and exercised
  pause/resume/cancel. A 390×844 fixture with 51 retained schedules measured a
  192-pixel scrollable list and a visible composer ending at pixel 844. This is
  component/layout evidence, not production-chat acceptance.


## Rollout and review gates

Migration `0047_recurring_wakes` adds three scheduler tables and nullable
`Worker.recurring_origin`; existing workers retain null ancestry. It is additive,
with no data backfill. Older code ignores these additions. No production schema
or runtime flag was changed during validation.

Draft PR only. Independent meaningful review and green CI are required before any
merge decision. No merge, production migration, restart, deployment, image dispatch
or infrastructure/secret change is performed by this task. An ordinary future
merge can auto-deploy Ergonaut and run additive migrations through its existing
init containers; account for that schema effect during review. Roll back code
without reversing the additive migration; reversal deletes schedule history and
requires a separate backup/approval decision.
