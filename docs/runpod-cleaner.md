# Local Runpod cleaner supervision

This is **preparation**, not permission to rent, deploy, download weights, or make
paid model calls. The runner does not create a Pod. Use the same existing SQLite
experiment ledger, REST ownership profile, resource receipts, and explicitly
selected scoped credential callback as the worker/recovery flow. No installation,
credential relocation, new account selection, service deployment, or restart is
needed. Lee confirmed operation/video use in permitted countries at22:42;
the LEGAL TEAM owns review at22:43. No pending legal question is assigned to
engineering, and this is not a legal-clearance claim.

## Two independently running processes

From the repository root, use the configured existing ledger. `LEDGER` and
`OFF_POD_EXPORT` below are operator-selected local paths; they must refer to the
same ledger and off-Pod export directory in both terminals. The ledger must
already contain its singleton reservation and immutable REST create profile.
Do not replace, truncate, or recreate it to recover a failed attempt.

**Terminal A — independent cleaner:**

```sh
export LEDGER=/absolute/path/to/existing-ledger.sqlite3
PYTHONPATH=src python -m django_ergo.plugins.runpod.supervisor "$LEDGER" \
  --interval-seconds 1 --operation-timeout 5 --freshness-seconds 10 \
  --execute --credential-env "$SCOPED_CALLBACK_ENV"
```

`SCOPED_CALLBACK_ENV` contains only the **name** of the already-selected scoped
environment variable in this cleaner process, not its value. Do not pick a new
credential source, print its value or put that value in command arguments.
The existing cleanup CLI
continues to instantiate `RunpodREST(lambda: os.environ.get(name))`; it obtains
the value only when cleanup HTTP is due. A fresh execute-mode heartbeat proves
configuration/liveness, **not** successful authentication or provider availability.

**Terminal B — worker:**

```sh
export LEDGER=/absolute/path/to/existing-ledger.sqlite3
export OFF_POD_EXPORT=/absolute/path/to/off-pod-export
PYTHONPATH=src python -m django_ergo.plugins.runpod.supervisor "$LEDGER" --check-fresh
PYTHONPATH=src python -m django_ergo.plugins.runpod.runner work \
  .worktrees/production-input/test-ready.zip.part001 \
  .worktrees/production-input/test-ready.zip.part002 \
  --ledger "$LEDGER" --output "$OFF_POD_EXPORT" \
  --setup-timings "$OFF_POD_SETUP_TIMINGS" \
  --base-url http://127.0.0.1:30010
```

These are runnable command shapes for the later authorized operation; **do not
run them as part of local preparation**. The worker command can make inference
requests to its configured endpoint. No credential or inference endpoint is
needed to run the deterministic mocked test suite.

The probe is not a replacement for per-shot gating: the worker calls
`CleanerSupervision(ledger).require_fresh(now=...)` before admission and each
shot. A missing configuration/heartbeat, another heartbeat from the worker's own
PID, a dry-run heartbeat, a failed cleaner tick, a future timestamp, or age
**greater than or equal to** ten seconds blocks admission. All state lives in
`cleaner_supervision` inside the same durable ledger. Tests may explicitly use
`require_execute=False` for dry-run fixtures; real worker admission must not.

To inspect scheduling without performing provider requests, omit `--execute` and
`--credential-env` from Terminal A. This dry-run cleaner may persist expiry/stop
intent but cannot terminate resources, and its heartbeat **does not qualify the
worker to start**. Do not use it in place of an executing cleaner for a rental.

## Deadline and failure behavior

- Supervision requires the immutable reservation deadline to be no more than
  3,600 seconds after creation. It never moves that deadline or renews a work
  lease. The runner's fixed work cutoff leaves its cleanup margin; it is not an
  extension of the rental deadline.
- Each supervisor tick commits a heartbeat and evaluates the same hard,
  work-lease, idle, and queue-cancellation expiry rules as `Experiment`.
  The next sleep ends at the earliest persisted deadline, even between ordinary
  one-second ticks. Worker process death or a shot blocked in inference does not
  prevent this independent process from recording stop intent and cleaning.
- Once stopped, one existing `cleanup` CLI child is run with a five-second
  operation timeout. Python kills and reaps that direct child on timeout. HTTP
  timeouts are not absence evidence: a deletion may already have succeeded.
  The next tick reconciles the durable receipts/profile instead of replaying a
  create. Errors and operation timeouts are sanitized in the ledger.
- Cleanup retries indefinitely while compute/storage absence remains unknown.
  Known-ID `GET` 404 is compute evidence, not universal storage evidence; only
  the existing immutable zero-optional-storage request plus verified ownership
  read supplies the existing Pod-disk storage proof. Unknown creates, foreign
  resources, and unverified storage never become success just because inventory
  is empty. No create command exists in the supervisor.
- The supervisor keeps watching after known absence, because a late receipt
  can reopen cleanup. Stop it only after the operator has reviewed the final
  durable outcome and all late-receipt uncertainty. Restart with the **same**
  ledger and timing configuration; no intent replay is needed or permitted.
- One local advisory lock (`LEDGER.cleaner.lock`) excludes a second supervisor.
  It is not a distributed lock. The three timing values are bound in the ledger;
  freshness must be strictly greater than operation timeout plus tick interval.

The $3 plan remains an **estimate with margin**, not an absolute zero-overrun
promise. Independent cleanup improves the deadline response but cannot guarantee
provider-side termination or billing within an hour during provider failures,
credential failure, OS suspension, wall-clock jumps, disk failures, or loss of
both processes/the host. If the supervisor dies, its heartbeat expires and new
worker admission fails; this alone cannot delete an already running Pod. Keep
the cleaner independently alive and monitor/restart it against the same ledger
if it exits. Provider failures can require manual reconciliation; unknown
compute/storage state must never be reported as a completed cleanup.

## Deterministic fixture coverage

`tests/test_runpod_supervisor.py` adds local SQLite, mocked REST, and process
fixtures for freshness/dry-run rejection, immutable timings, deadline-aware
wakeups, child timeout/retry, unknown create/storage evidence, credential-name
forwarding, singleton locking, late monitoring, and a worker killed mid-shot
before independent hard-deadline cleanup. The HTTP fixtures forbid networking;
the killed-worker fixture synchronizes over a pipe rather than waiting for a
real rental clock. The bounded-child fixture only sleeps in a local Python
process. No provider calls, secrets, weights, or installation are involved.

## Optional existing process supervisor configuration

`examples/runpod-h3/supervisord-cleaner.conf` prepares a foreground independent
cleaner with bounded child ticks and restart-on-exit. It contains no credential
value or credential-file path and is **not installed**. Use an already available
Supervisor installation, the approved existing callback environment, and
operator-selected `REPO_ROOT`, `PYTHON`, `LEDGER`, `CLEANER_LOG_DIR` and
`SCOPED_CALLBACK_ENV`; paths used in the command must not contain whitespace.
Create the log directory before starting it. Replace Terminal A's command with:

```sh
supervisord -n -c examples/runpod-h3/supervisord-cleaner.conf
```

Keep Terminal B independent, without the provider credential in its environment.
The worker never supervises/restarts this cleaner and this configuration never
auto-restarts generation. A durable host/operator must supervise the supervisor;
the config cannot recover a failed host or guarantee provider billing finality.
