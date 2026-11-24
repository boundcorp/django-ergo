# Admission drain (experimental, default off)

`DJANGO_ERGO["ADMISSION_DRAIN_ENABLED"]` defaults to `False`. Leave it off in
runtime configuration. This is an admission integration for offline validation,
not a supported rollout switch: when enabled, `ergonaut upgrade` acquires a
bounded lease, observes idle under that lease, and **returns without calling the
external upgrader**. The current upgrader interface cannot retain fencing through
workload replacement. The legacy upgrade path remains unchanged when disabled.

The database migration creates a singleton lease. Acquisition, renewal and
admission serialize on its row lock. Expiry uses PostgreSQL's clock after the
lock wait, renewal cannot exceed the acquisition's fixed maximum lifetime, and
only the matching token can release it. An abandoned owner's lease expires;
its token cannot release a replacement acquisition.

Each admitted execution also holds a shared PostgreSQL advisory lock on a
separate session for its entire execution. This makes the interval before a
`StructuredCall` or `Worker` claim visible to the idle observer after the admission
transaction commits. Cleanup closes that connection; process death releases it.
Nested runtime calls register separately and inherit admission only for the same
session while the outer registration remains active. Copied context from an ended
execution cannot admit new work. A direct connection is required: external
transaction-mode connection pooling is unsupported. Each active boundary uses an
additional database connection, so connection capacity must be reviewed before
any future activation.

## Consumer behavior

| Consumer | During a lease | Recovery |
| --- | --- | --- |
| Web/inbox turn | Host session admission precedes inbox consumption; each subsequent inbox batch checks again | Inbox remains intact; the existing stranded-inbox sweep dispatches it |
| Approval answer, stop-paused answer, revalidation | Retry before looking up or consuming the pending decision | Celery retry retains original task arguments and approval identity |
| Channel turn, including Telegram durable intake and approval recovery | Wait asynchronously before the host lock callback and source claim | Pending source rows remain pending; cancellation cleans up admission |
| Thread-message delivery, broker or local thread | Return before `_claim_next` | Message stays queued; existing redispatch paths retry it |
| Direct `Bot.ask` / `Bot.resume` | Wait asynchronously before creating a call or consuming approval | Caller retains request; cancellation releases registration |
| New durable Worker step | Return `draining` before `_claim`, preserving polls, status and next-poll data | Existing overdue-worker sweep schedules it again |
| Already-started Worker or external agent watcher | Continue steps and expired-step recovery naturally | Existing step lease and completion fencing remain in force |
| Coding-agent launch | Wait before manager validation/launch | Caller retains request; the launch and watcher creation stay registered |
| Celery `@bot_task` / schedule workflow | Retry before execution or the schedule claim | Original task arguments / pending `ScheduleRun` remain intact |
| Library-local background task / workflow | Wait before execution or schedule claim | Existing in-process caller/future remains responsible for the request |

An external agent already exists before its first watcher poll. Consequently
`agent:*` and legacy `orca:watch` watchers count as existing work even while their
Worker row is queued. During drain preparation, genuinely unstarted queued
Workers do not prevent idle; started Workers, registered executions and the
existing local-job gate do. Outside drain preparation, `wait_idle` still counts
all queued Workers. No active work is cancelled to make a drain finish.

Drain preparation renews its 30-second lease on each observation, at most five
seconds apart, and bounds acquisition lifetime to 30 minutes. Timeout and errors
release the matching lease; process death relies on TTL. Blocking database or
filesystem calls can still exceed a local polling deadline; expiry restores
admission and prevents treating that observation as a valid handoff.

## Remaining activation boundary

The current code deliberately refuses every external upgrade while the option
is enabled. Before replacing that refusal, implement and test atomic handoff
ownership, renewal through workload replacement, stale-coordinator rejection,
rollback/failure recovery, and authenticated readiness for the exact immutable
runtime revision and required workload generations. A second lease check before
an external restart request would leave another expiry/handoff race and is not
sufficient.

The tests use disposable PostgreSQL databases, actual lock contention, a child
process exiting without cleanup, TTL expiry, real consumer claim/requeue paths,
and fake model/agent functions. They do not prove live Redis/Celery broker
redelivery, whole-host loss, database-connection loss while application code keeps
running, external transaction pool behavior, or rollout/rollback behavior. Local
in-memory inboxes/futures retain their existing process-loss limitations. No
migration of those queues into durable storage is implied. Auxiliary standalone
model jobs (such as naming/illustration), voice bridge claims and arbitrary
third-party plugin execution are outside this turn/worker admission contract;
they need explicit policy and tests before any broader shutdown guarantee.

No deployment notifications or deployment-live event API are included here.
