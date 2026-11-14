# Lifecycle events and evidence reports

The lifecycle ledger is opt-in (`DJANGO_ERGO["LIFECYCLE_EVENTS_ENABLED"] = True`), independent of OpenTelemetry's provider spans and the private-inference cost ledger. It stores opaque IDs, controlled enums, retry numbers and timestamps, not message text, tool arguments, results, question bodies, account names or transcripts. The recorder is best-effort and isolates database errors in a savepoint: a missing table or invalid observation does not fail the originating work. Event loss is possible, including a process crash between a committed transition and its on-commit observation. Absence is **unknown**, not evidence that a stage never happened. Existing observations cannot be updated via the normal model and queryset APIs; retention can delete them. This is not a tamper-proof database audit log against privileged SQL.

The ORM guard makes ordinary `save()` calls insert-only: even a newly
constructed instance with an existing explicit primary key cannot overwrite
that row. An insert with a duplicate primary or unique key fails with a
database integrity error. Saving a loaded event, using update options on a new
event, queryset `update()` or `bulk_update()`, and `bulk_create()` with
`update_conflicts=True` are rejected. `update_or_create()` can create a new
event but cannot update an existing one. Normal bulk inserts and
`ignore_conflicts=True` remain available; ignoring conflicts can silently skip
an insert.

This is an application-level safeguard, not a database-enforced append-only
policy. Low-level APIs such as `save_base()`, private ORM methods, raw SQL and
privileged database access can bypass it. Model and queryset deletion remain
available for retention, and deleting then inserting a row is not prevented.

## Observations and correlations

`record_event(stage, key=..., ...)` appends at most one observation per globally unique key. Use a stage and the stable opaque object ID plus retry or poll identity where appropriate. Reusing a key does not modify the first event. UUIDs, integers and bounded hexadecimal tokens only; never put content in IDs or keys. The controlled vocabulary includes worker, handoff, approval, question, dependency, download, image, Gradle, iOS/EAS, compile, test, package, CI dispatch/queue, deploy, retry and cache categories; cache hit/miss and cold/warm are explicit measured states. Actors include model, provider, agent, worker, infrastructure, human, Lee, system and unknown; execution, idle and stall are distinct states. Absence of a heartbeat proves neither activity nor inactivity. The vocabulary is not a claim that those categories are hooked.

`occurred_at` is the time the originating boundary was observed; `observed_at` is the ledger insert time. Out-of-order inserts are valid; retries are separate events. Reports order by occurrence, observation and key. With `USE_TZ=False`, naive database values use Django's default time zone; with `USE_TZ=True`, explicit occurrence times must be aware. Events after the report's supplied `as_of`, including late observations, are excluded. A duplicate key with contradictory data in a supplied report input is rejected.

Current hooks:

- Thread messages: persisted queue, accepted dispatch, recipient claim (`delivered`), initial turn input persisted (`started`), first successful engine response (`resumed`), and persisted reply. `dispatched` is not an acknowledgement; a successful response proves the turn reached the engine, not semantic acceptance of a particular instruction. Each message is its own task, linked by message UUID, sender/recipient session UUIDs and reply-to message UUID. A reply link is correlation, not a dependency.
- Workers: creation, durable scheduling, claimed step, explicit progress, completion/cancellation and successful persisted result, correlated by worker and session UUID. Worker poll attempts identify retries. Finished and persisted observations follow the same durable save and are not an independently measured interval. A `worker_finished` message's first successful follow-up engine response correlates back to the worker as `worker_result`/`resumed`; messages merely queued or claimed are not consumed.
- Tool approval: persisted pending approval records a blocker with a deterministic hash of call UUID and tool-use ID. A successful approval claim records `answer_received`; persisted tool results record `resumed` and `blocker_resolved`. The pending row does **not** prove that a card was displayed to Lee, that Lee was the decision maker, or that an approval was avoidable. Identity and surfacing stay unknown. A claim may be recorded even if subsequent resume fails; it is not the same as consuming the answer. A transport `acknowledged` event is separate and never implies an answer or consumer resume.

Existing events and transcripts are not backfilled. No generic human question/answer, card surfacing, external transport acknowledgement, worker-agent semantic acceptance, build/CI/deploy/provider timing, heartbeat, cache measurement, or dependency hook is asserted by this slice. Explicit `dependency_added`/`dependency_removed` events are available to future producers; they are not inferred from reply links. A routine inquiry, new scope, deliberate pause, repeated approval and ambiguous state can be represented as separate controlled observations, but are not inferred from wording or later activity. Authorization is unchanged.

## JSON projection

```python
import json
from django.utils import timezone
from django_ergo.models import LifecycleEvent
from django_ergo.conversation.lifecycle_report import project_lifecycle

report = project_lifecycle(LifecycleEvent.objects.filter(task_id=task_id), as_of=timezone.now())
print(json.dumps(report, sort_keys=True))
```

Choose a bounded, authorized set of correlated IDs; filtering on one task does not automatically discover descendants or workers. The result includes an ordered timeline, per-task blockers, per-worker result status, reply correlations and explicit dependency edges. A Lee-attributed human interval requires an explicit `blocker_created` with actor `lee` and state `actionable_surfaced`, whose producer must establish both a task-blocking request and actual surfacing to Lee. The first answer ends that interval; answer-to-resume is system delay. A closure without an answer is not an answer. Missing or contradictory boundaries are unknown. `result_unconsumed` means no later qualifying follow-up observation in the selected data, not proof that no other consumer exists.

`pending_age_seconds` measures the age of an observed unresolved blocker; it is
not a human-wait or whole-thread-idle measurement. An `answer_received` ends
human wait but does not establish that the blocked consumer resumed, and an
unrelated task's answer or transport acknowledgement cannot resolve it.

Task blockers do not imply thread inactivity. Intervals may overlap independent work and are not additive. The critical path is deliberately unknown without a complete dependency graph; no bump causality, pre-existing authorization, user idle time or avoidable-approval claim follows from these events alone. Limit access to reports: even opaque identifiers and timing reveal operational relationships.

## Retention, rollout and rollback

Migration `0045_lifecycle_event` is additive and must be applied before enabling the setting. Ergonaut enables it with `ERGONAUT_LIFECYCLE_EVENTS=1` (default: off). Changing a deployed service requires the normal coordinated change window. This PR does not apply migrations, deploy, restart services or schedule jobs. Assess table/index growth, backup retention and access controls before enabling. Existing application code remains functional if observations fail, but missing observations cannot be reconstructed reliably.

Schedule `manage.py prune_lifecycle_events --days 90 --batch-size 1000` under the deployment's normal operations policy. It deletes **one** oldest batch by `observed_at`, never newer than the configured age; repeat on a bounded schedule until caught up. Days must be positive; batch size is capped at 10,000. Default retention is 90 days only when the command is actually scheduled; there is no autonomous cleanup. Backups and downstream exports need their own retention. Pruning truncates histories and prevents complete reports across the cutoff.

Rollback: turn the flag off and roll back code under the same deployment policy. The additive table may remain for retention/export; reversing migration 0045 deletes all ledger history and requires separate explicit approval and a backup decision. Older code ignores the table. No production migration or restart is authorized by this documentation.

## Offline overhead sample

One local isolated pgserver run with test-only durability settings (fsync and
synchronous commit disabled) measured 500 unique `started` calls: disabled
0.326 ms total; enabled 1,307.104 ms total, or 2.614 ms per recorded event;
projecting the 500 persisted rows and 500 tasks took 31.025 ms. This is a single
offline observation, not a production latency or throughput guarantee. A
production database, concurrent writers, indexes and storage durability can
change the cost; measure locally before enabling broadly.

## Authorized real-task trace

After a coordinated rollout, select one explicitly authorized, non-sensitive task privately. Capture its known task/message, thread, worker, turn and question identifiers in the private investigation workspace; query only those IDs and the permitted time window. Compare each event against existing authoritative worker rows, message claims and turn/approval records, then export only the content-free JSON report to the approved private location. Verify enqueue versus claim versus actual follow-up processing; distinguish approval claim from resume and explicit surfacing from unknown. Preserve gaps as unknown, report any dropped observations, and never paste private identifiers, transcripts or infrastructure details into the public repository. This procedure does not authorize starting a paid task, rollout, or accessing unrelated histories.
