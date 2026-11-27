# PR #416 review remediation — draft, mock only

Real paid launch status: **REFUSED**. Production confinement/runtime and rollout
are absent. The approved $1 total OpenAI smoke within $25/week (Lee, Nov 27 at
20:08) has not been executed; no additional smoke approval is requested.

Original owned head: `56fd9c127f096fd3009c61a9cbdd77d1be04c002`.
Final base: fetched main `64fbee1`, including merged #407 (`c0d63aa`) and #414.
Only this owned worktree was edited; the #407 merge worker's checkout and the
independent review checkout were not modified. PR #416 remains a draft.

## Remediation

- **P1-1:** removed the settings/dotted-class launch authorization path. Neither
  a backend's claimed `prepare()` prefix nor a static enable/verification flag
  can authorize execution. The production gate unconditionally refuses before
  process/capability creation until a separately reviewed, evidence-backed runtime
  exists. Ambient provider keys are also refused. This fixes unsafe enablement;
  it does not claim live confinement has been implemented.
- **P1 harness:** a real local bubblewrap user/PID/mount/network namespace, with
  capabilities dropped, denies parent fake key/proc and credential-file access
  and direct TCP to an external mock provider listener/synthetic external IP.
  Both the sandbox and its descendant reach an external mock proxy via the only
  mounted Unix socket. The ordinary `env`/same-UID leak is retained as a negative
  control. These are OS boundary tests, not an Octo deployment attestation or
  proof of TCP/TLS proxy Service reachability.
- **P2-1:** source pending markers are a durable accounting outbox. Ergonaut beat
  retries 100 pending rows every minute; failures rotate behind older work,
  preserving fairness. Source locks and ledger identities make replay/concurrent
  ingestion idempotent. No provider operation is retried. Pending counts are
  reported in the API/coverage manifest with owner and accessible-bot filtering;
  source exposure remains charged while reporting is unavailable. The expiry
  recovery task is scheduled as well. Live scheduling is not deployed/verified.
- **P2-2:** completed Responses and `incomplete/max_output_tokens` terminal usage
  settle only when usage, model, tier and reservation checks pass. A legitimate
  128-token truncated output is priced. Missing/partial/bool/negative/inconsistent
  usage, content filtering, failure, duplicate/mismatched terminal evidence,
  post-terminal data, stream cuts, wrong model and overruns remain unknown holds.
- **P2-3:** integrated actual merged #407. `ProviderAccountMapping` is the single
  audited identity source; the second settings map is removed. Real coding
  `Decision` authority and the same provider/pool/owner weekly checks reserve
  coding allocations and proxy attempts; ordinary recurring CLI launches remain
  refused. Launches capture their week, new attempts refuse after rollover,
  Sunday settlements remain in the old week and unknown holds carry forward.
  Tests use actual $25 weekly consent as well as a $25 pool cap, with mock sends.
- **Other review notes:** implausible zero input usage retains a hold, hosted tool
  choices refuse, restricted coding privacy is rechecked at the proxy, and a
  matching owned allocation is released when the absent-runtime gate refuses
  before any process or token exists. Launched/in-flight allocations are untouched.

## Integration and schema

Main's #407 migration is `0065_provider_weekly_budgets` after
`0064_isolated_analysis`, so the proxy migration is **0066_paid_agent_proxy**.
The graph is linear; up/down and drift checks exercise this graph.

The substantive settlement rebase conflict was resolved by preserving #407's
provider/envelope lock order and performing the durable ledger transition inside
those locks. Later #414 appended analysis cleanup at the same task-file seam;
both analysis cleanup and paid-accounting tasks were retained. Migration tests
retain the analysis ancestry checks and assert the new 0066 leaf.

Combined tests exposed two historical migration checks that use current budget
helpers at schema 0065: selecting the entire current request model attempted to
read the not-yet-created paid proxy columns. Weekly totals now select only their
accounting inputs, preserving 0065 migration/reversal reads without changing
weekly spend arithmetic. The #407 leaf assertion was extended to 0066 while its
0064→0065 data assertions remain intact.

## Verification

Every test command unsets real OpenAI, Anthropic and Fireworks keys and uses
`ERGO_TEST_ISOLATED=1 TEST_OPENAI=false`; transports are mocks or loopback and
credentials are fake strings. The throwaway PostgreSQL clusters are local tests.

Final command results are recorded below. Validation transcripts
are retained under [validation](validation/).

- Focused acceptance/accounting/confinement/weekly/migration/adapter suite:
  **101 passed**; includes 0066 down/up and `makemigrations --check --dry-run`.
- Explicit $25 consent seam: **2 passed**.
- Historical schema regression checks: **3 passed**.
- Migration leaf plus standalone admission module: **15 passed**.
- Post-#414 Ergonaut API/cost plus analysis-settings suite: **202 passed,
  16 skipped**.
- Full final library (`pytest tests -q -n 6 --tb=short`): **3838 passed,
  15 skipped, 1 failed** in 531.68 seconds. The sole failure is
  `test_admission_paths.py::test_direct_async_admission_waits_and_cancellation_releases`
  (`assert not executions_active()`).
- Untouched current-main `64fbee1` in a separate temporary worktree, same command:
  **3728 passed, 15 skipped, 1 failed** in 440.23 seconds. That run fails
  `test_real_worker_claim_gap_remains_registered`, also an `executions_active()`
  assertion in the same admission module. This is not a reproduction of the
  identical test at current main; the independent review did reproduce the
  draft's exact direct-async failure on earlier main `0896fb9`. The admission
  module passes in isolation here (included in the 15-pass run above). The
  underlying registry/timing cause is unverified and no unrelated admission fix
  or weakened assertion was introduced. **The full suite is not claimed green.**
- Ruff lint/format and diff checks: passed (650 Python files formatted).
- Remote PR #416 was confirmed draft at the original head; its existing frontend,
  chat-ui and lint checks were successful. The publication target remains draft
  #416 on `leewardbound/paid-agent-accounting-test`; prior-head CI does not certify
  this revision. The dispatched completion records the verified pushed commit.

The review's original tests and report were read-only. Reproducers were copied
into `tests/test_pr416_review_acceptance.py`, corrected into acceptance expectations
and extended with adversarial SSE and concurrent retry tests. The two earlier
implementation reports are preserved as explicitly superseded historical snapshots.

## Remaining live blockers and proposed approvals

The runbook [docs/paid-agent-proxy.md](../../docs/paid-agent-proxy.md) contains the
exact proposed infra-octo paths, isolated workload/policy/TLS/RBAC scope, and a
launch-time attestation/lifecycle contract. No manifests or shared infrastructure
were changed. Its proposal is a new keyless sandbox namespace and separate proxy
namespace/service, sandbox-only restricted egress and no ambient host mounts,
provider credentials, service-account token or Docker socket.

A reviewed controller must create, challenge-probe and execute in the same retained
sandbox, bind evidence to pod/policy/image/proxy identities, and fail closed on
any missing or failed evidence. Backend self-report cannot satisfy that contract.
The local Unix-socket harness is not such a production controller.

Separate Lee/infra-owner approval is required for new workloads, scoped network
policies/RBAC/TLS, proxy-only credentials, migration/config and load-bearing rollout.
Do not change global pods/policies or existing runtime credentials as a workaround.
A future operator must verify actual deployed enforcement, audited account mapping,
weekly consent/funding/remaining exposure, exact tariff and maintenance deployment
before the already-approved smoke. Paid calls, merges, deploys, restarts, policy,
credential and funding changes were not performed by this worker.

## Environment evidence

Read `~/ENVIRONMENT.md` first. `envcheck` was not initially on PATH; the documented
installed route `/home/dev/bin/envcheck` succeeded: Octo/home readyz ready; rigel,
macaroni, enterprise and vega reachable; `/dev/kvm` writable. No unreachable-service
claim is based on the PATH miss. Existing unsafe Octo runtime facts are from the
independent read-only review, not a newly asserted isolation deployment.
