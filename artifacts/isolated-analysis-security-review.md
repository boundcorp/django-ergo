# Independent isolated-analysis security review

Date: 2026-11-27. Draft PR: https://github.com/boundcorp/django-ergo/pull/408 . Reviewed input: `107808445831dbbc07422e94d7ffd5b9c20f61ec`, including foundation `3e463b799f9f06ef8d8f84500734739854d0e7c3` and implementation `78866be`. Fixes are additive on the same branch; the input history is preserved.

**Readiness: code-review candidate only; keep disabled.** No merge, deploy, restart, provisioning, live migration, credential handling, image build/push, paid API calls or live-runtime tests occurred. `/home/dev/ENVIRONMENT.md` was read first. No host, tool or service is claimed unreachable, so no availability claim depends on a missing envcheck. Tests use disposable embedded PostgreSQL and fixture authentication keys.

## Findings and fixes

| Severity | Finding and location | Fix / evidence |
| --- | --- | --- |
| High | `analysis/controller.py:reconcile` released the workspace fence after API disappearance, including a running pod lost with its node; graceful delete plus 404 did not prove process death. A new transfer could then race surviving arbitrary Python. | Pod creation now includes a termination finalizer. `analysis/kubernetes.py:status` requires explicit executor termination/finished-at evidence and rejects synthetic NodeLost/ContainerStatusUnknown states. Controller retains fences on unexplained disappearance, ambiguous missing launches and unconfirmed termination. Terminal results require durable termination evidence plus API absence. Regression covers launching/running/reaping disappearance, restart, cancellation and retention fencing. |
| High | Reaping state/result were committed after deletion; a controller crash during deletion could lose both the result and knowledge that execution had stopped. | Persist reaping intent, result and a `stopped` bit before delete/finalizer removal. Restart recovers that evidence. Legacy private ledgers add the column conservatively with zero; no old absence becomes evidence. Tests simulate crash immediately after finalizer removal and upgrade an old ledger. |
| Medium | Runner image working directory is writable `/workspace`; `python -m` could import attacker workspace modules instead of the executor or its dependencies. This stays within the pod but breaks the intended separation between arbitrary scripts and subsequent structured operations. | Pod command and both image entrypoints use `python -I -m`. A real subprocess test plants shadow `json.py` and `django_ergo` modules and verifies isolated module startup ignores them. No arbitrary uploaded code is executed locally. |
| Medium | Dataset result validation allowed arbitrary schema entries and counts beyond the database field range. Select validation checked version and merely increasing offset, allowing malformed or huge cursor claims to become signed app receipts. | New `analysis/results.py` validates bounded schema names/types/uniqueness and integer row counts; selects must match compiled output columns, dataset source count, row widths/scalar types, result byte/row limits, total/count/truncation and exact next-offset consistency. App integration tests verify rejection before dataset persistence. Validation is not an attestation of result truth. |
| Medium | Attachment protection could change after initial import lookup without changing `updated_at` (for example a QuerySet update). The per-chunk checks did not explicitly recheck `private_export_id`. | Every chunk now explicitly refuses newly protected copies as well as checking source ACL/storage/hash/version. Regression applies the flag after initial lookup without updating timestamp and verifies no job is launched. Existing same-session protected-copy and cross-session ACL regressions remain. |
| Medium | Cancelling an ambiguous submission whose request never reached the controller failed with Unknown job; retrying to establish its identity would launch work despite the cancellation request. | Cancel carries the persisted canonical request while the app receipt is submitting. Controller records a permanent cancelled job identity without launching when no matching job exists. Delayed submits return the cancelled receipt; conflicting identities and foreign-workspace job ids are rejected. Tests cover controller and app paths. |
| Low | Python source was placed in the child's command-line arguments, making source available to command-line telemetry. | Source goes to isolated child stdin. PodSpec environment and private app/controller ledgers still contain request content; these require access, logging and retention controls. This is documented explicitly. |
| Low | Existing analysis integration imports violated `tests/test_import_layers.py`, a suite omitted from the previous acceptance command. | Moved the app integration service from `analysis/service.py` to `plugins/analysis_service.py`, updating callers/tests/docs. The layer check now passes without new exceptions. |
| Low | App job/dataset/workspace lookups accepted malformed UUID strings into ORM validation rather than applying the controller's canonical-ID contract. | Validate IDs before ORM lookup. Added malformed-handle and revoked-session tests for submit/status/cancel/select/import/export. |

## Boundary audit

- Every public app operation reloads session ownership and applies `closed_to`; calls additionally bind workspace, session and user. Job/dataset reads scope to the exact session, including another session owned by the same user. Attachment import reuses the configured attachments plugin ACL and refuses protected export copies. File listing, imports, exports and structured parsers execute only as sandbox jobs. Export creates a separate attachment after reauthorization; it never uses arbitrary output as an application filesystem path.
- The app/controller never import executor/pandas/DuckDB or parse dataset formats. Workspace names are flat, descriptor opens reject symlinks/nonregular/hardlinked/oversized files, writes use exclusive creation and contiguous offsets. Transfer leases and the active-job fence serialize scripts and transfers; node-loss fixes preserve that premise. Chunks have actual streamed-byte caps, offsets, stable version and final checksum checks.
- Arbitrary Python shares the executor UID/process namespace and can access parent descriptors via `/proc`, kill/interfere with the parent or forge a complete executor JSON response. No key lives in that pod, but the trusted controller will legitimately sign its **untrusted content**. HMAC authenticates transport/controller identity, not sandbox calculation integrity. Application receipt validation prevents malformed values becoming capabilities/cursors; a malicious workspace can still return internally consistent false results. Sandbox outputs never authorize another session, execute app/controller code or choose arbitrary application paths. Exact-count/provenance fields describe the normal query algorithm, not tamper-proof scientific evidence.
- HMAC covers canonical bounded JSON, version/time/nonce and request-bound replies. Durable replay consumption precedes effects. Job identity binds request bytes and workspace; ambiguous submissions retry the same identity. The actual HTTP harness exercises valid requests, replay, wrong key, expiry, malformed/oversized bodies and incorrect endpoint. The app client remains HTTPS-only with certificate verification, ambient proxies disabled and bounded streamed replies; the local HTTP harness does not validate production TLS/ingress.
- Kubernetes job manifests mount one dedicated workspace PVC and bounded private tmpfs, with no service-account token, secret/hostPath/socket mount, host namespaces or service links. They require pinned images, RuntimeClass, fixed nonroot UID, read-only root, dropped capabilities, RuntimeDefault seccomp and CPU/memory/ephemeral-storage bounds. Runtime/network/PID/hard-storage/log enforcement remains a separately approved deployment prerequisite.
- Reconciliation excludes active jobs and unexpired leases from retention, durably tombstones ownership before storage deletion, and retries asynchronous deletion after crashes. Existing tests cover result-loss and PVC-delete crash recovery. Independent exported attachments survive cleanup. Ledgers, Kubernetes PodSpecs/audit data, backups and logs need their own content-access and retention policy.
- Structured queries quote schema identifiers, bind scalar predicate values, cap tree depth/node/membership complexity, restrict operations, and bound columns/rows/bytes. They query the full frame and verify content versions on each query. Cursors bind dataset/workspace/version/query hash/offset and expire. The reference tests exercise full-data filters/counts/aggregates, nulls, sorting, CSV/Parquet/Excel, paging and mutation/version rejection. Whole-file memory and recomputation remain intentional limitations.

## Kubernetes API semantics checked

An actual installed Kubernetes Python client **37.0.3**, configured only against an ephemeral loopback HTTP emulator, exercises PVC read/404/create/delete, Pod create/read/log/delete/patch and typed response deserialization. Tests assert camelCase `limitBytes`, `gracePeriodSeconds=1`, `propagationPolicy=Foreground`, JSON Patch media type, UID/resourceVersion tests, retained finalizer and eventual 404. Initial harness failure was an emulator MIME error: Kubernetes logs are text/plain; returning a JSON content type made the client deserialize the log as a Python dict string. Corrected the emulator and the real-client wire test passes.

Proposed RBAC now includes Pod patch for finalizer removal; nothing was applied. Finalizer removal preserves unrelated finalizers and uses optimistic concurrency. The controller requires a single durable process/replica; running two independent ledgers/controllers is unsupported.

References checked during review: [Pod termination](https://kubernetes.io/docs/concepts/workloads/pods/pod-termination/), [Pod lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/), [unreachable-node toleration behavior](https://kubernetes.io/docs/concepts/scheduling-eviction/taint-and-toleration/). These explicitly explain why API deletion/node eviction does not guarantee that a process stopped. Local emulation proves client serialization behavior, not actual kubelet/runtime behavior.

## Validation commands and results

- Original three analysis suites at input SHA: **80 passed**, 9.70s.
- Initial broader import-layer check reproduced a real failure: **1 failed, 1 passed**, caused by the two analysis service upward imports; fixed by relocation.
- Intermediate combined acceptance plus independent/layer regressions: **339 passed, 8 skipped**, 93.29s. This preceded the last import-shadowing/revocation/legacy-ledger tests and the foreign-job cancel guard.
- Final focused analysis + security + import-layer suite: **118 passed**, 15.08s, after the final code changes and added regressions.
- Final combined acceptance: **347 passed, 8 skipped**, 92.12s, after all code changes and regressions.
- `/tmp/ergo-analysis-venv/bin/ruff check .`: passes across the repository. `ruff format --check .`: 625 files formatted.
- `ERGO_TEST_ISOLATED=1 TEST_OPENAI=false /tmp/ergo-analysis-venv/bin/python -m django makemigrations django_ergo --check --dry-run --settings=tests.example_app.settings`: no changes detected.
- Same interpreter/environment, `python -m django check --settings=tests.example_app.settings`: no issues.
- Proposed YAML safely parses as one List with eight resources; `git diff --check` passes.
- Remote main `39b99c183784cbff474e7cf1bd84322d13218128` was fetched/read. Its latest migration is still `0063_google_engine_signatures`; dependency of `0064_isolated_analysis` is correct. No rebase/merge or live migration performed.

Final combined command:

```sh
ERGO_TEST_ISOLATED=1 TEST_OPENAI=false /tmp/ergo-analysis-venv/bin/pytest \
  tests/test_isolated_analysis_contract.py tests/test_analysis_runtime.py \
  tests/test_analysis_service.py tests/test_analysis_security_review.py \
  tests/test_import_layers.py tests/test_bot_plugins.py tests/test_agent_skills.py \
  tests/test_conversation_attachments.py tests/test_remote_attachments.py \
  tests/test_private_exports.py -q
```

All test runs report the pre-existing platformdirs XDG_RUNTIME_DIR fallback warning. Eight skips belong to the existing repository suites. This is a relevant combined suite, not a claim that all repository tests ran.

## Remaining runtime gates

Keep `ERGO_ANALYSIS_ENABLED=False`. Separate infrastructure approval and real-runtime adversarial acceptance are still required for sandbox worker/RuntimeClass, deny-all networking including IPv6/metadata/node routes, finite quota-backed volumes and reclaim policy, PID/process/log/disk enforcement, reviewed immutable images, admission on creates/updates/ephemeral-container subresources, controller-only credentials/HTTPS, and privacy-safe telemetry.

Explicitly test finalizer retention on cancellation/timeout/node loss and node deletion, kubelet termination-status reliability, runtime cleanup of all descendants, crash between ledger commit and finalizer removal, and eventual CSI reclamation. API absence without prior recorded termination deliberately blocks the workspace indefinitely; unscheduled/unconfirmed launches may also require an independently verified operator fencing procedure. No automatic override is provided. Never clear a ledger fence or finalizer merely to restore availability; there may be surviving Python. Privileged administrators/runtime/kubelet/control plane and correct storage isolation remain trusted; compromised infrastructure can falsify status and is outside this broker's proof.

Unrun: live Kubernetes/container tests, image builds/scans, network/process/resource/disk adversarial tests, production TLS verification and runtime acceptance. Existing limits remain: one controller replica, 16KiB-per-pod transfers, whole-file pandas, first-sheet Excel, pandas-only joins, offset recomputation, polling and no resumable transfers. These are documented limitations, not evidence that isolation works.
