# Model pools C1–C4: core and UI remediation

## Disposition and exact scope

Core implementation: `2fe7ab3`, appended to #404 `46edcfc` without rewriting its history.
UI implementation: `a29dd5b`, five #405 commits rebased cleanly onto the core implementation in
`/home/dev/orca/workspaces/django-ergo/model-pools-funding-ui` (original #405 head `6c965dc`).
The final core branch additionally carries this report, test logs and the Google handoff patch;
#405 is rebased onto that final core head before its draft push. The shared/main branch is untouched.

**Core/UI local acceptance is green. Combined Google integration is still a blocker.**
The Google and reviewer worktrees were read only. No merge, deploy, restart, shared runtime,
credential/config change, production consent/funding record, paid inference or paid coding job
was performed. Local databases, users, account labels and approvals are synthetic.

## Implemented controls

### C1: funding exists independently of configuration

- A config-created envelope stays unfunded. No active config grant is issued before an
  authenticated owner/admin explicitly supplies a positive cumulative USD cap, future expiry
  and nonempty exact account/model set. The UI leaves all three authorization inputs empty.
- `SpendFunding` persists each approval, actor, window, account identities and note independently
  of providers.yaml. `SpendControlAudit` records funding, administrative continuation/preparation,
  restoration and paid pause actions. Core checks the actual active user and superuser status;
  a caller-provided `admin=True` alone cannot link history or approve funding.
- Config grants intersect explicit consent with current ceilings and routes. Config changes
  cannot add consent or enlarge the approved cap/window/account set. Lowering and later restoring
  a ceiling may return only to the independent approved limit; an already-built guard remains
  intersected with the grant it captured. Account identity includes the exact model.
- Funding revisions retain the same envelope and cumulative ledger. Owner and child grants
  continue sharing the family cap; priced use, pending reservations and unknown holds survive.
  Human revocation survives funding changes and continuation; only distinct admin restoration
  lifts it. Restore alone creates no funding.
- Unapproved legacy rows deny. Migration 0062 infers no consent, activates enforcement for
  existing envelope history and starts those installs paused. The #406 orphan-history gate from
  `8ab1e22` is now in standalone core, with an acceptance test: parentless issued history prevents
  automatic creation of a new config family when its money cannot be identified.
- Funding and grant amount validation rejects ambiguous precision beyond four decimal places,
  nonfinite/negative/boolean values and excessive amounts. Config monetary ceilings are rounded
  down to grant precision. Funding caps are positive and below $100,000,000; submitted windows
  must be timezone-aware. Unknown/empty account sets and extra API fields are refused.

### C2: captured and current privacy both constrain each send

Decisions capture immutable original policy restrictions and bot/folder lineage. Guards retain
session identity, recorded session origins and inherited turn origins. Every outbound guard
check reloads current config, checks current session origins and current policy coverage of
captured/current lineage, intersects original restrictions, and denies removed policy names.
Current inherited origin is also checked at send. Queued policies persist their original
restrictions and lineage, so reload/resume cannot erase them. Tests cover new bot restrictions,
new session origins, disappeared policy names, captured restrictions and inherited send origins.

### C3: exact transport/account fencing

`conversation.transport_identity.identity` returns a non-secret identity of provider type,
transport, credential-source name, declared account, effective endpoint and model. Default key
sources resolve to the selected environment variable name, never to its secret value. Guarded
routes refuse inline credentials and endpoint URLs containing credentials, query or fragment.

A decision freezes this identity; its guard compares current config at each authorization and
reservation. Queued work records route identities and refuses a changed account on resume.
OpenAI and Claude freeze their construction identity and effective endpoint, call
`MeteredGuard.check_transport` before each attempt, and check the SDK client's actual endpoint.
Runtime construction verifies that the engine matches the decision. Mocks verify that both real
adapter entry points refuse stale identities before their fake outbound callbacks are reached.
Credential values are not compared, logged or included in approval/audit identities.

This establishes configured identity fencing, **not independent proof of a real provider billing
account**: changing a secret's underlying account while retaining the same declared source/account
cannot be verified without a separate account inventory. No real credentials or accounts were tested.

### C4: durable enforcement and paid pause

The singleton `PaidDispatchState.activated_at` is latched on first governed pool activation,
and conservatively by migration when envelope history exists. Removing pools cannot restore
unguarded paid routing after activation. OpenAI/Claude unguarded attempts also check the durable
state immediately before dispatch. Missing/unreadable state fails closed. Healthy, never-activated
legacy installs retain compatibility. Admin pause retains schema, approvals, grants and all holds;
unpause does not add money or lift revocations.

Rollback documentation now requires a paid-dispatch pause with schema/ledgers retained.
Schema reversal is only an isolated maintenance/backup procedure under an independently enforced
paid-egress pause across all roles: older code and a removed latch schema cannot enforce the new
pause. Do not remove pools, clear switches or serve pre-guard code to regain capacity.

## API/UI changes in #405

- `GET /api/pools/{name}/funding`: owner/admin-only pending state, configured ceiling, existing
  approval, exact selectable account/model identities and cumulative family spend/holds.
- `POST /api/pools/{name}/funding`: explicit cap, expiry, account selection and optional start/note;
  actual authenticated owner/admin validation, strict extra-field rejection, no consent defaults.
- `POST /api/pools/paid-pause`: strict boolean, authenticated admin only; audit and retained ledgers.
- Existing admin `GET/POST /api/pools/{name}/envelope` remains separate. Continue existing family,
  prepare a separate family/account linkage, and restore are mutually exclusive operations;
  preparation alone now supplies **no** spend consent. Explicit approval is still required.
- Pool settings include funding-pending/configured-ceiling information for owners, and paid pause/
  activation state. Ordinary users and grantees cannot read another owner's funding/accounts or
  call funding/reconciliation endpoints; grantees see their own grant, not owner consent details.
- Routing has empty explicit funding inputs, unchecked account choices, cumulative use/unknown-hold
  context and separate admin recovery controls. Admins can open controls by configured pool name,
  including another owner's pool without adding it to an ordinary user's listing. The pause control
  is a code-only workflow; no production action was taken. Existing grant issuance/revocation stays
  separate from funding. UI forms never prefill actual authorization from config or prior approvals.

## Migration graph and Google integration blocker

Core leaf is linear:

`0058_agent_launch_intent → 0059_model_pools → 0060_spend_envelopes →
0061_spend_envelope_families → 0062_explicit_funding_and_paid_latch`.

0062 creates `PaidDispatchState`, `SpendFunding`, and `SpendControlAudit`. Existing envelopes
cause a paused, activated latch; no funding is backfilled and ledger rows are not changed.
The isolated upgrade/reverse/backfill tests preserve a synthetic unknown hold across 0062 reversal
and re-upgrade. Reversal removes funding/audit/latch tables, so it is not a normal operational rollback.

#406 at `8ab1e22` still names `0062_google_engine_signatures`. **It must be renamed to
`0063_google_engine_signatures`, depending on core 0062, before combination.**
`artifacts/model-pools-funding/google-followup.patch.gz` prepares that rename and Google constructor/
per-attempt identity and unguarded-latch hooks. The decompressed patch applies to the exact 8ab1e22 source files
and compiles in a disposable scratch tree; it is **not applied or integration-tested**. No Google
worktree or branch was edited. The next integration must also update migration-leaf assertions and
Google test setup to explicitly fund synthetic pools, then rerun the full combined suite and adapter
adversarial tests. A Google engine with a mismatched construction identity must deny even when
someone attaches an otherwise-valid guard; changed-config rejection alone is not this test.

## Verification ledger

All Python execution used `ERGO_TEST_ISOLATED=1` and isolated embedded Postgres. Core suite used
this checkout's `.venv`; host/API suite used its existing `ergonaut/.venv` with this core source and
the owned UI source explicitly on PYTHONPATH. The rebased UI's `src/` and `tests/` are identical to
core, and its Ergonaut files are byte-identical to the pre-rebase tested UI tree. Post-rebase focused
API checks were rerun against the rebased UI's own source paths. No dependency installation or
shared runtime was used. Evidence is in `artifacts/model-pools-funding/`.

| Check | Result | Evidence |
| --- | --- | --- |
| Full core `pytest tests -n 4 -q --ignore=tests/test_model_pools_review.py` | **3347 passed, 15 skipped**, 311.32s | `core-full.txt` |
| Focused pools/authority/acceptance plus bot/provider/import-layer regressions | **355 passed, 1 skipped** | `core-focused.txt` |
| Expanded independent C1–C4 acceptance | **32 passed, 1 skipped** | `c1-c4-acceptance.txt` |
| Upgrade/reverse/backfill dedicated | **3 passed, 51 deselected** | `migration-up-down.txt` |
| All host bot/API tests | **275 passed, 1 skipped**, 155.80s | `bots-api-all.txt` |
| Post-rebase funding + pool API/review | **26 passed** | `funding-api-post-rebase.txt` |
| Frontend tests | **416 passed, 0 failed**, 41 files, 1704 assertions | `frontend-tests.txt` |
| Frontend TypeScript and production build | **Pass**, existing large-chunk warning | `frontend-tsc.txt`, `frontend-build.txt` |
| Core and Ergonaut migration drift | **No changes detected**, both | `*-migration-drift.txt` |
| Ruff, format, pre-commit and diff whitespace checks | **Pass** | commit hooks / local commands |

The independent review file was copied read-only from the reviewer checkout, inverted to acceptance,
and expanded; its Google-specific case is skipped in core because core intentionally has no Google
adapter. The historical defect-asserting `tests/test_model_pools_review.py` was excluded as in the
independent review. These counts describe core/#405, **not** the prior 3430-test Google combination.
An initial broad run exposed 22 test-fixture/metadata/import-layer failures, all corrected before the
final green run; no current core failure is being treated as a baseline exclusion. The bot/API suite
warned about one isolated test DB connection at teardown, without failures. Existing runtime-dir,
static-directory and SDK-test warnings remain.

## Remaining blockers and product scope

1. Apply/review the Google follow-up in an authorized integration checkout, update explicit-funding
   test setup and migration leaf assertions, and validate the exact combined heads. Until then the
   Google migration graph conflicts and constructor/unguarded-send hooks are not integrated.
2. Independently re-review the new persisted-consent, privacy, identity and pause semantics. Passing
   mocks do not certify every provider, race, billing account, subprocess or plugin egress path.
3. Paid coding remains unsupported: shipped managers' confinement/spend-bound flags are unchanged.
   No bounded paid coding adapter, native subprocess certification or paid test is delivered here.
4. There is no general unknown-outcome reconciliation/release workflow. Unknown holds remain held;
   the UI displays them but never clears or refunds them. Parentless history may require an audited
   operator reconciliation; the controls do not infer that decision.
5. No live account/tariff/usage validation, production consent inventory, rollout, operator pause,
   activation, browser usability certification or paid multimodal/server-tool certification was done.
   Actual funding decisions belong to the owner/admin; this implementation builds controls only.

## Environment

Read `~/ENVIRONMENT.md` first. `envcheck` was attempted and returned
`/usr/bin/bash: line 1: envcheck: command not found` (127). No host, cluster or service was declared
unreachable; no host/cluster access was needed. No production route was attempted or mutated.
