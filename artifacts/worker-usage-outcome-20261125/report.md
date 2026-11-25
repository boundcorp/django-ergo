# Worker outcome and exact native usage evidence — 2026-11-25

## Disposition

Partial implementation, production native accounting blocked. PR402's original
b7dfc031e59c2b0c393e01b45cf32aae5c28239f is preserved. This continuation completes
explicit outcome emission guidance, coordinator visibility and permission-scoped
structured weekly assertion counts. It adds a separate source-compatible native
metadata consumer, not production attribution or authenticated execution proof.

## Completed code

- `orca_instructions.CHECKPOINT_GUIDANCE` tells native workers to emit exactly
  one complete JSON payload with taskId, dispatchId, outcome and work_disposition.
  The CLI rejects combining raw `--payload` with structured flags: it does not
  merge them. The documented example retains exact Dispatch scope and uses a
  successful transport with a partial work assertion to illustrate independence.
- `workers.result_for_chat` puts the explicit field first even after PostgreSQL
  JSONB has reordered keys. An 11KB report cannot hide it behind the 6000-character
  coordinator preview. The full stored worker result retains the field.
- Weekly self-health projects only `result__work_disposition` from already
  authorized/non-personal selected sessions and counts four allowed strings plus
  unknown. Missing, malformed, unrecognized and legacy/prose-only results stay
  unknown. Transport status counts are separate; no prose parsing occurs.
- `orca_native_usage.collect(shown, dispatch_id=..., expected_host=..., home=...)`
  accepts only trusted exact-dispatch native identity plus an attested closed
  fresh/exclusive byte interval. It requires the mapped host, confines reads to
  the native agent home, checks file device/inode and exact-byte digest, verifies
  Claude row session IDs or Codex session_meta ID, and emits token metadata only.
  Claude request/message keys dedupe streaming updates; Codex keys include the
  timestamp so identical counts at different times are not lost. Cumulative-only
  Codex counters become deltas. Missing counters are null; missing timestamped
  usage is unavailable, not zero. No transcript storage or model calls occur.

The metadata consumer is deliberately unwired: it cannot authorize a caller or
invent the missing runtime export/profile. Its caller must check owner/session
permissions before a host read. No new ledger ownership links or thread totals
are created by this PR.

## Production-source evidence and prerequisite

The installed `orca orchestration worker-show --dispatch <this-dispatch> --json`
returned successfully with dispatch, worker, terminal and observation, but **no
nativeUsageIdentity**. Only the presence/absence of fields was inspected; no native
session files were read or copied. `envcheck` was attempted first and returned
`/usr/bin/bash: line 1: envcheck: command not found` (exit 127); the documented
local Orca CLI route itself succeeded, so this is not a host-unreachable claim.

Existing clean upstream worktree `orca/dispatch-native-session-usage`, branch
`leewardbound/dispatch-native-session-usage`, already contains:

- `645f6a08883`: verified native session observations.
- `86aac0598df`: stricter Dispatch projection and ownership contract.
- `be3a3ea8acf`: fresh exclusive intervals.

No matching PR was found by exact head against stablyai/orca. This existing work
must be coordinated, not duplicated. Its production source contracts are:

- `src/main/runtime/orchestration/workers/transcripts/dispatch-native-usage-capture.ts`:
  `captureDispatchNativeUsageIdentity` accepts only live admitted hook observations,
  cross-checks `getExactWorkerProviderSession`, launch-token hash, pane, process,
  agent, native session ID and receipt timestamp, then records against current
  Dispatch authority. Replays and unconfirmed/restored observations are excluded.
  `settleDispatchNativeUsageIntervalAtRelease` verifies the same session/process
  again before closing the interval; shared/resumed sessions fail closed.
- `src/main/runtime/orchestration/db/dispatch-context/dispatch-native-usage-identity.ts`:
  conditional Dispatch authority update, immutable first identity and permanent
  unavailable state on identity change, plus other-Dispatch session exclusion.
- `src/main/runtime/rpc/methods/orchestration/worker/observation/dispatch-context-exposure.ts`:
  `exposeNativeUsageIdentity` emits sessionId/hostScope/process/pane/launch hash,
  explicitly unavailable profile/segment attribution and a closed exclusive
  interval only when it exists. Observation alone is not usage ownership.
- `src/cli/orchestration/workers/worker-view-results.ts`:
  `WorkerShowResult.nativeUsageIdentity` and `NativeUsageInterval` define the
  exact shape consumed here. Older runtimes omit the field.
- `src/cli/orchestration/messages/message-payload.ts`:
  `getOptionalStructuredMessagePayload` throws `Use either --payload or structured
  payload flags, not both.` This explains why an additive payload flag fails.

These are real upstream implementation paths, not invented parser fixtures.
Their existence does not mean the installed runtime shipped them. The current
legacy worktree/time scanner was not repurposed as exact identity evidence.

## Concrete integration request for durable launch worker 6b45

Coordinator was informed during implementation (messages msg_4c599e5a500a and
msg_b03d1a10e103). Question/blocker: who owns publication of the existing upstream
branch, and can the durable launch worker retain the native export in its handle
and watcher state without treating transport delivery as authenticated acceptance?

Proposed minimal integration, after upstream export is reviewed/shipped:

```python
# On each trusted worker-show response, not from the worker's self-report payload:
receipt = {
    "dispatch": {"id": shown.get("dispatch", {}).get("id")},
    "nativeUsageIdentity": shown.get("nativeUsageIdentity"),
}
# Persist only this metadata in durable state/handle; never terminal previews.
ctx.state["native_usage_receipt"] = receipt
# At settlement, fetch worker-show AGAIN after Orca closes its release interval.
# The pre-release response has no closed interval and cannot be reused as final.
# Authorize ctx.worker.session owner and resolve hostScope -> files_host using
# configured host identity; a cwd, host label guess or time window is insufficient.
metadata = collect(
    receipt,
    dispatch_id=handle["dispatch"],
    expected_host=verified_host_scope,
    home=authorized_native_home,
)
```

For a remote files host, execute the stdlib consumer on that host and return only
its JSON metadata; do not transfer native transcript bytes. The source module has
no CLI wrapper yet, so the SSH adapter must serialize the trusted receipt and
explicit host binding, with no inheritance of worker-supplied arbitrary paths.

Before attaching events to AgentRunSession/AgentUsageEvent, establish verified
profile identity and owner scope (upstream currently says profile_not_verified),
reject shared/resumed sessions and cross-worker ownership conflicts, and enforce
idempotent per-session event keys. Keep unknowns visible; no default profile or
reconstructed session ID. No launch-path edits were made here, and no deploy,
merge, restart, drain activation, credentials or native CLI upgrade was attempted.

## Validation

All runs use `ERGO_TEST_ISOLATED=1`, a unique disposable embedded PostgreSQL
instance; no shared application/test DB. Deterministic synthetic Claude and Codex
fixtures only, with no LLM/agent inference or paid fallback. Test/check results
and final commit/PR evidence are reported with completion below.

Validation completed: isolated PostgreSQL
`pytest tests/test_orca_native_usage.py tests/test_self_health_metrics.py tests/test_orca_scope.py tests/test_bot_workers.py tests/test_orca_instructions.py tests/test_import_layers.py -q`
passed **207 tests**. The first long-report preview test exposed JSONB ordering;
the renderer fix above resolved it. Focused Ruff checks/format and
`git diff --check` passed. No model/provider calls were used.
