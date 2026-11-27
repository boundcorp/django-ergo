# Isolated analysis implementation validation

PR: https://github.com/boundcorp/django-ergo/pull/408
Branch: `leewardbound/isolated-analysis-workspaces`
Foundation preserved: `3e463b799f9f06ef8d8f84500734739854d0e7c3`.

Implemented a disabled-by-default, session-authorized HTTP broker/controller and actual credential-free Kubernetes job executor; lazy Django workspace/job/dataset models and migration; async Python/query lifecycle; streaming attachment import/export; parameterized full-data select/filters/grouping/aggregates/sorts with authenticated version/query-bound pagination; seven opt-in bot tools and two library skills. Added proposed image recipes and namespace/network/quota/RBAC policy; no infrastructure was applied. Design, limits, migration, rollout/rollback and exact infrastructure prerequisites are in `docs/isolated-analysis-plan.md`.

## Validation environment

Read `/home/dev/ENVIRONMENT.md` first. Reconciled remote draft PR 408 and branch at the preserved foundation commit before editing; re-fetched and verified zero divergence before preparing the final push. Created a temporary test virtualenv at `/tmp/ergo-analysis-venv`; used Django 5.2.19, Ruff 0.12.5, DuckDB 1.4.4, pandas 2.3.3, pyarrow 26.0.1, openpyxl 3.1.5 and Kubernetes client 37.0.3. `ERGO_TEST_ISOLATED=1` uses disposable embedded PostgreSQL, not Ergonaut's live database. No paid API/model calls were made.

## Checks

- Initial preserved foundation tests: 13 passed.
- Intermediate combined repository/plugin/skill/attachment suite: 273 passed, 8 existing skips, one platformdirs runtime-directory warning.
- Final focused analysis + private-export regression run: 111 passed, one platformdirs warning. This includes the later source checksum, protected-copy laundering refusal, child timeout and crash-safe storage deletion regressions.
- Combined acceptance suite: **308 passed, 8 skipped**, one platformdirs warning, in 88.58 seconds; includes the three analysis suites plus bot plugins, agent skills, conversation attachments, remote attachments and private exports.
- Last analysis regression suite after rejecting case-ambiguous dataset/aggregate names: **80 passed**, one platformdirs warning, in 10.26 seconds. These three analysis suites cover the final query/executor change; the combined suite above preceded that final added regression.
- Ruff check passes for all changed Python application modules and new tests; Ruff formatting applied. Optional runner/controller dependencies are also included in the development extra so the existing `[dev]` full-test CI job runs these regressions rather than failing with missing imports.
- `manage.py makemigrations django_ergo --check --dry-run`: no changes detected after additive migration `0064_isolated_analysis`.
- `manage.py check`: no issues.
- `git diff --check`: passes.
- Proposed YAML safely parses as one Kubernetes List containing eight resources; nothing applied. The repository single-document YAML hook rejected the original multi-document form, so the proposal was converted to a List rather than bypassing the hook.

Acceptance command:

```sh
ERGO_TEST_ISOLATED=1 TEST_OPENAI=false /tmp/ergo-analysis-venv/bin/pytest \
  tests/test_isolated_analysis_contract.py tests/test_analysis_runtime.py \
  tests/test_analysis_service.py tests/test_bot_plugins.py tests/test_agent_skills.py \
  tests/test_conversation_attachments.py tests/test_remote_attachments.py \
  tests/test_private_exports.py -q
```

Offline fixtures verify authenticated bounded/replay-protected request/reply envelopes, malicious query handling, every predicate/aggregate, exact full-data counts, all three readers, deterministic cursor pagination/version changes, session/user/handle authorization, configured attachment ACLs, protected private-export refusal, path/link attacks, actual streaming caps/checksums, independent exported attachments, active/transfer/stale cleanup, controller restart and storage-delete crash recovery, cancellation, timeout, malformed/lost output reaping, retry idempotence, job PodSpec controls, disabled/lazy behavior and actual plugin/skill registration.

## Unrun checks and limits

Real container/Kubernetes isolation smoke and adversarial tests are unrun; image builds/pushes, infrastructure creation, live migrations, merge/deploy/restart and enabling existing bots were not performed or authorized. No mocked runtime is presented as proof of isolation. No host or service is reported unreachable.

Runtime enablement requires separate Lee approval for a sandbox RuntimeClass/worker pool, verified deny-all networking, quota-backed 1 GiB workspace volumes, kubelet/runtime PID enforcement, hard node/log disk quotas, restricted admission/RBAC, verified HTTPS/authentication and a durable single-replica controller ledger. Pod ephemeral-storage limits are eviction thresholds, not hard quotas. App processes never launch code/parsers or receive runtime credentials. Node-loss fencing and actual process/storage reclamation must be accepted on the target runtime before enabling.

First version limitations: protected private-export copies deliberately cannot enter reusable workspaces (avoids unprotected derived exports); one job/workspace and one controller replica; whole-file pandas parsing can hit memory/time limits; first XLSX sheet only; no structured joins (isolated pandas joins available); offset queries recompute full data; 16 KiB transfer chunks launch separate pods and have high startup overhead; polling rather than terminal notifications; no resumable transfer tool. Workspaces expire after configurable idle retention, while exports remain independent chat attachments. See the architecture doc for exact settings and rollback order.

## Independent review follow-up (2026-11-27)

Input SHA `107808445831dbbc07422e94d7ffd5b9c20f61ec` was independently audited against actual code. See `artifacts/isolated-analysis-security-review.md` for findings, fixes, adversarial tests, wire-protocol evidence and remaining infrastructure gates. This follow-up supersedes the original disappearance-based reaping safety claim: API absence does **not** prove process termination. Durable termination evidence and a retained Pod finalizer now protect the workspace fence; unexplained absence remains blocked.

The application service moved to `plugins/analysis_service.py` to fix an actual repository import-layer regression. Added dataset/select result validation, isolated module startup, source-free child argv, per-chunk private-export rechecks, canonical handle validation and cancellation tombstones for pre-launch ambiguous submissions. Arbitrary Python can still forge executor content; transport signatures do not attest calculation correctness. Feature defaults remain disabled.

Independent checks:

- Reproduced the original final analysis result: **80 passed**, 9.70s.
- Reproduced and fixed the previously omitted layer suite failure (two upward imports).
- Intermediate expanded acceptance: **339 passed, 8 skipped**, 93.29s.
- Final focused analysis/security/layer regression run after the final code edits: **118 passed**, 15.08s.
- Final expanded acceptance: **347 passed, 8 skipped**, 92.12s, after all code changes and regressions.
- Repository-wide Ruff check and format check pass (625 Python files), migration drift has no changes, Django system check has no issues, proposed YAML parses as eight resources and diff whitespace passes.
- Fetched current main `39b99c183784cbff474e7cf1bd84322d13218128`; migration dependency `0063_google_engine_signatures` remains its latest schema.
- Actual loopback HTTP harness verifies controller authentication/replay/errors. Actual generated Kubernetes client 37.0.3 exercises adapter wire serialization and responses against a local emulator, including JSON Patch finalizer removal. Neither test validates deployed TLS, Kubernetes admission, kubelet cleanup or runtime isolation.

The expanded command adds `tests/test_analysis_security_review.py` and `tests/test_import_layers.py` to the original combined command above. Existing eight skips and the platformdirs runtime-directory warning remain. No live infrastructure/image/deployment tests or paid API calls occurred.
