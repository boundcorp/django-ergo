> Historical implementation snapshot. Superseded for current status and validation by [review-fixes.md](review-fixes.md).

# Paid coding-agent accounting implementation — 2026-11-27

Draft review only. No paid inference, merge, deployment, restart, deployed
configuration/funding/account mapping changes, credential provisioning or copying
occurred. The earlier worker's `report.md` and four mock tests are retained as
baseline evidence. The standalone ledger inclusion fix is commit `8795688`;
its two regressions failed against main `0896fb9` and passed with the fix.

## Delivered code

- Mandatory durable cost-ledger ingestion for recorded paid `api` and `metered`
  InferenceRequest attempts, idempotent source identity, reservation/release
  separated from monetary estimates, and unknown holds preserved. Updated the
  former opt-in coverage contract and its library/API assertions. Historical
  rows remain available through the adapter, without automatic production backfill.
- A separate threaded WSGI OpenAI Responses/Chat proxy with fixed upstream,
  authenticated expiring per-launch tokens (only digests stored), account/grant/
  user/model bindings, current SendAuthority tariff and switch checks, atomic
  per-attempt reservation and launch attempt/cumulative reservation ceilings,
  request IDs, actual models and token evidence. Streaming is forwarded as SSE;
  interrupted/malformed/missing usage retains the complete reservation.
- PaidAgentLaunch schema and migration `0065`; crash-expiry accounting command;
  supervisor and watcher revocation; an opt-in `paid_codex` manager sharing the
  existing local supervised path. Other paid managers remain refused.
- Minimal child environment, fresh CLI home/config directories, no real API keys,
  explicit custom Codex provider/token, no inherited CLI login, and fail-closed
  admission without an external confinement backend. Presence checks refuse
  workspace .env/login/config files and ambient direct endpoint/alternate-key
  overrides. Mock backend evidence is not an isolation claim.
- `docs/paid-agent-proxy.md`: deployment/service contract, exact #407 weekly
  integration seam, not-run $1 total / three-attempt smoke inside Lee's $25/week
  family authority, and provider reconciliation/access requirements.

## Proven by unpaid tests

The tests exercise real PostgreSQL envelope and launch locking with httpx mock
upstreams: exact settlement; Responses and Chat streaming; per-request retries;
parallel launch-cap and cross-launch shared-grant races for the last reservation;
cap/model/provider/pause/grant/token/account/key/base-URL refusals before the
mock transport receives any request; unknown holds on cut, unread, closed,
partial or missing streams; and actual-model mismatch evidence and revocation.
The supervisor test runs a fake Codex executable and verifies the actual child
environment contains only the proxy capability, not either provider's key.

An additional test runs the installed **codex-cli 0.171.0** against a loopback
HTTP server connected to the real proxy and a mocked provider. It completes one
Responses turn with a mock request ID and exact local settlement, with no real
provider key in its environment. Initial probing found and fixed a missing
CODEX_HOME directory plus the actual CLI's inline namespaced local-tool request
shape. The proxy strips inline stored IDs and encrypted-response expansion,
recursively refuses hosted tools, and bounds the full local schemas. This is
real CLI/mock transport evidence, not a paid run or a network-isolation test.

## Live blockers and scope limits

1. There is **no verified Octo OS/filesystem/process/egress backend** in this repo.
   The documented read-only Octo route found no NetworkPolicies in `ergonaut`;
   shared process uid/home access is not a boundary. `paid_codex` therefore
   refuses before token issuance/process launch unless trusted host code provides
   one. No backend was deployed or configured. See the runbook for exact checks.
2. The separate proxy service/private TLS routing/key-only service environment
   has not been deployed. The proxy URL is intentionally absent from normal
   Ergonaut web URLs. A threaded WSGI runtime is required for its sync SSE iterator.
3. Current main enforces all-time grant/family authority, **not $25/week**. #407's
   weekly period must be integrated atomically into shared reserve/totals and
   stored on launches/attempts, with cross-week tests, before the live smoke.
   Neither its branch nor its monthly-provider-budgets worktree was touched.
4. No actual org/project/key-id mapping, exact live model tariff, provider
   billing permission or remaining Lee budget was verified or changed. The new
   mapping setting must be operator-audited and tied to existing funded authority.
5. One CLI turn is proven against mocks; actual provider behavior, isolated
   multi-turn/tool operation, real network bypass attempts, live disconnects and
   provider billing reconciliation remain unproven. Unsupported/encrypted input
   and returned-model/tier/rate uncertainty fail closed or retain unknown holds.
6. Historical rows are not backfilled automatically, external paid sources
   without InferenceRequest are still explicitly outside cost-ledger coverage,
   and estimates remain **UNVERIFIED CHARGES** until provider evidence exists.

## Environment and operations

Read `~/ENVIRONMENT.md` first. `envcheck` returned command-not-found; the exact
explicit kubeconfig route to Octo succeeded for pod and NetworkPolicy reads.
No secrets or .env contents were printed. Credentials were checked by presence
only, apart from fake strings created in synthetic tests. No real key was
provided to the mock CLI or any test transport. Initial local pytest collection
at repository root picked up a historical artifact requiring unrelated Celery;
the actual library suite is `pytest tests`, matching CI.

## Validation

Draft PR: https://github.com/boundcorp/django-ergo/pull/416 (OPEN, draft).
Implementation commit: `73e4af0d00607e053e38ad42ed728533b21239db`.

- Final full library run: **1 failed, 3644 passed, 15 skipped**, 7 warnings,
  368.52 seconds. Command:
  `ERGO_TEST_ISOLATED=1 TEST_OPENAI=false env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u FIREWORKS_API_KEY uv run --extra dev pytest tests -q -n 4 --tb=short`.
  Output: `/tmp/paid-clean-full-library.txt`. This is explicitly **not an
  all-green full-suite result**.
- The sole final failure is the unchanged
  `tests/test_admission_paths.py::test_direct_async_admission_waits_and_cancellation_releases`:
  its immediate post-cancellation `executions_active()` assertion sees True.
  It failed in both parallel full runs and passed the isolated rerun. The
  admission implementation and its test were not changed by this branch;
  A full-module isolated rerun then passed **13 tests** in 14.02 seconds;
  the underlying timing cause remains unverified. This is a remaining
  broad-suite validation issue, not suppressed or marked xfail.
- Earlier parallel full run: **3 failed, 3639 passed, 15 skipped**, 384.26
  seconds. In addition to that same admission failure, it caught a new upward
  resolver import (fixed by moving the resolver to `bots/paid_agent_authority.py`)
  and a stale hardcoded migration leaf (updated for 0065). Their focused rerun,
  together with the admission case, was **4 passed** in 10.34 seconds.
- Superseded early serial run (modules collected before the manager/account
  mapping and migration-leaf fixes): **2 failed, 3637 passed, 15 skipped**,
  1280.94 seconds. Both failures were fixed and absent from the final parallel
  run. A second redundant serial attempt was interrupted with coordinator
  approval after the required final parallel run completed.
- Final focused mock/accounting/manager/CLI set: **72 passed**, 1 warning,
  45.19 seconds. Command:
  `ERGO_TEST_ISOLATED=1 uv run --extra dev pytest tests/test_paid_agent_proxy.py tests/test_paid_codex.py tests/test_metered_cost_hooks.py tests/test_paid_agent_accounting_mock.py tests/test_inference_cost_hooks.py tests/test_cost_adapters.py -q`.
  Includes installed Codex CLI against mocks; that case separately passed
  **1 test** in 8.55 seconds. No real paid provider transport was used.
- Ergonaut API plus cost projection tests: **190 passed, 16 skipped**,
  247 warnings, 45.18 seconds. Command uses `ERGO_TEST_ISOLATED=1`,
  `TEST_OPENAI=false`, scrubbed OpenAI/Anthropic/Fireworks key variables,
  `PYTHONPATH=ergonaut:src`, `DJANGO_DEBUG=true`, and
  `pytest ergonaut/ergonaut/api/tests ergonaut/ergonaut/apps/bots/tests/test_costs.py -q -n 4 --ds=ergonaut.settings.test_settings`
  under `uv run --extra dev` with Ergonaut's optional runtime dependencies.
  An earlier expanded run had one obsolete `live_ingestion=False` expectation;
  it was corrected for mandatory ingestion, and this complete selected set
  passed on rerun. The API-only earlier run was **162 passed, 16 skipped**.
- `ruff check .`: passed. `ruff format --check .`: **635 files already
  formatted**. Git diff whitespace and all pre-commit hooks passed.
  PR checks `lint`, `frontend`, and `chat-ui` passed on the implementation head.
- On a disposable isolated PostgreSQL database, migration **0065 up, down to
  0064, up to 0065 again** all passed; `makemigrations --check --dry-run` reported
  **No changes detected**. No deployed database was touched.

Warnings were the existing runtime-directory fallback, asgiref callable,
pytest-asyncio configuration, factory/Celery deprecations and short test JWT
keys. No warning is presented as proof of provider billing or live isolation.
All validation commands redirected output to files instead of piping away test
exit status.

Two redundant serial validation attempts were superseded by the full parallel
run; after coordinator approval, only the still-running worker-owned pytest PID
was interrupted with SIGINT (the other had already finished). No unrelated
process or deployed workload was interrupted.
