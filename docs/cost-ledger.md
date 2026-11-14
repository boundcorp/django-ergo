# Cost ledger: core design and adapter contract

## Decision and boundaries

The ledger is an owner-scoped accounting observation store, not a spend
permission system. No provider calls, financial dispatch/retries, credentials,
historical ingestion/backfill, rate discovery, or new budget enforcement run from it. Existing private
inference, Storytime central Ads, and native RunPod authorities remain authoritative.
An estimate is never an invoice; reservation/release is never a charge. Unknown is
NULL, not zero. Currency is explicit; there is no implicit FX or pooled cap.

Two additive models in the existing `django_ergo` app:

- `CostOperation`: stable economic identity `(provider, account_ref, operation_key)`;
  immutable owner, currency, bot/project/job association and optional provider
  resource identity `(provider, account_ref, resource_type, resource_id)`.
  `operation_key` identifies one nonoverlapping billable component/period, not a
  worker. Resource identity survives worker replacement. Compute and storage use
  separate operation keys; a subscription invoice uses its own operation.
- `CostObservation`: immutable source identity `(provider, account_ref, source,
  source_event_id)`; economic operation, kind, explicit positive revision, event
  and local observed time, optional Decimal amount, generic measured units,
  normalized token usage, bounded safe provenance, and session/worker/tool/attempt
  references. Resource references may first appear on a later observation after
  uncertain create; the operation lock rejects a different subsequently claimed
  provider resource without rewriting earlier history. Replaying identical normalized data returns the existing row;
  changed data under either source identity or `(operation, kind, revision)` is a
  conflict. Local observation time is not part of replay comparison.

A transaction locks the economic operation before inserting observations. Source
and revision uniqueness also hold in database constraints. Revisions are ordered
by source revision, not arrival time. Different producers must coordinate the
revision sequence for the same kind or use a single canonical adapter.
Provider/account/source names and economic period keys must be canonicalized by
the reviewed adapter. The ledger cannot infer that two different external keys
describe overlapping periods or aggregate copies. Never generate fresh keys to
make a failed replay succeed.

Projection selects one monetary observation per operation: final reconciled >
provider reported > latest estimate > unknown. Later estimates cannot downgrade
settlement. Superseded rows stay in history but never add to projected spend.
API-equivalent values, subscription coverage and reservation/release remain
separate informational categories. Totals group by category and currency with
known subtotal AND unknown count; all-unknown subtotals are NULL, not zero. Negative explicit
amounts can represent provider credits; adapters must not invent credits.

Owner deletion is protected while ledger history exists. Session and worker
references are opaque snapshots, not cascading foreign keys. API access requires
an authenticated matching owner, or an authenticated superuser for reviewed
cross-owner reporting/ingestion. Host routes must additionally enforce bot access.
Host adapters also validate that session/worker/job references belong to the
authorized context; opaque identifiers themselves do not grant access.
The ordinary ORM is a trusted administrative boundary, not an end-user API.
No raw provider bodies, prompts, URLs, paths, credentials, free-text errors, or
arbitrary metadata are accepted. Source IDs/account references must be safe opaque
identifiers (hash externally when necessary); code-valued provenance is allowlisted.

## Token and measurement contracts

Normalized input means **uncached** input. Cache write/read are disjoint input
buckets; output includes reasoning (reasoning is a subset, never added twice).
`normalize_tokens` requires explicit `total` versus `uncached` input semantics;
missing counts remain unknown, and impossible partitions fail. Generic units
include image_count, pixels, video_seconds, audio_seconds, gpu_seconds and any
bounded adapter-defined unit identifier. Quantities are Decimal or unknown, not
floats. No rates are invented. A price reference identifies reviewed provenance,
not proof of billing.

## Core API

Import models from `django_ergo.costs.models`, specs and enums from
`django_ergo.costs.types`, and functions from `django_ergo.costs.ledger`.

```python
from decimal import Decimal

from django_ergo.costs.ledger import record_observation, summarize_costs
from django_ergo.costs.types import CostKind, ObservationSpec, OperationSpec

# owner comes from trusted authenticated context, not request-body identity.
operation = OperationSpec(
    provider="example", account_ref="opaque-account",
    operation_key="provider-request-uuid", currency="USD", bot_name="example",
)
row, created = record_observation(
    actor=owner,
    operation=operation,
    observation=ObservationSpec(
        source="reviewed-adapter-v1", source_event_id="request-uuid-estimate-1",
        kind=CostKind.ESTIMATE, revision=1, amount=Decimal("0.0125"),
        provenance={"model": "exact-model-id", "price_reference": "reviewed-v1"},
    ),
)
buckets = summarize_costs(owner, bot_name="example")
```

`record_observation` returns `(CostObservation, created)`; `CostConflictError`
means inconsistent source identity, revision, attribution or resource. It rolls
back all local changes. A local recording failure NEVER authorizes resending an
external operation. Repair/replay accounting only from durable evidence.
An authenticated superuser may pass `owner=` for reviewed cross-owner imports;
ordinary owners cannot assign another owner.

`effective_observations(actor)` returns a QuerySet selecting one financial row
per operation. `latest_observations(actor)` selects one row per operation/kind.
Both apply ownership before ranking and encapsulate ranking in a subquery:
subsequent filters cannot resurrect a superseded estimate. Use the latter for
history-category displays, not to sum all categories as spend. Both return no
rows for anonymous or inactive principals; active superusers can see all owners.

`summarize_costs(actor, bot_name=None, project_ref=None)` returns sorted dicts
containing `kind`, `currency`, `known_amount` (Decimal or None), `known_count`,
`unknown_count`, `operation_count`. It includes effective monetary rows and
separate reservation/release/API-equivalent/subscription buckets. It omits
usage/lifecycle-only records: they are not evidence of a monetary amount or of
free usage. Adapters must emit an unknown or unknown-amount estimate when an
operation is potentially billable but unpriced. Empty output means no recorded
monetary status, NOT complete coverage, zero spend or budget headroom.

Amount and measurement precision is 38 digits / 18 decimal places (20 integer
digits); unrepresentable inputs fail rather than round. Amounts must be finite
Decimal values. Resource periods are either absent or fully bounded. Event times
must match Django's USE_TZ convention; aware times canonicalize to UTC. The
database assigns local `observed_at`, and missing `occurred_at` stays unknown.
There is no raw response, rate snapshot, credential or free-text notes column.
Provenance keys are limited to model, region, service, price_reference,
evidence_ref, funding, lifecycle, source_model, source_ref and parser_version.
Values are bounded codes/opaque references (model IDs may have namespace slashes),
not text. Funding values are metered_api, subscription, external_shared, unknown,
promo_credit; a funding label alone is never proof that a credit was applied.

## Offline verification coverage

`tests/test_cost_ledger.py` covers exact and conflicting replay, both source and
economic-revision identities, concurrent identical/conflicting inserts and
cross-operation source races, out-of-order revisions, settlement precedence,
safe post-projection filters, separate reservation/funding categories, currency
isolation, high-precision credits, explicit zero versus unknown, permissions,
protected ownership, constrained private payloads, token partitions and reasoning
subsets, bounded resource periods, late resource discovery and worker replacement.
No paid/provider probes or historical data are used.

## Adapter coverage and non-duplication

Coverage varies by source and configuration; no historical import is authorized.

| Source | Canonical identity and mapping | Coverage / authority |
| --- | --- | --- |
| InferenceRequest | one operation per request UUID; attempt UUID source event; reserved_usd is reservation; cost_usd derived from rates is estimate, never final | Private pool attempt hooks are opt-in; existing inference admission unchanged |
| StructuredCall / AgentUsage | aggregate/cumulative projections, references only; do not ingest alongside constituent requests as new costs | Unsupported charge sources |
| AgentUsageEvent / native CLI | canonical native request/delta key, not transcript/cumulative replay; API-equivalent and subscription funding explicitly distinct | Follow-on codex_cli owner; no chat extraction |
| Native RunPod | provider/account/resource type/provider resource ID; native job UUID, worker UUID, attempt/create-intent references; component/resource-period economic key | Follow-on RunPod owner; durable rental ledger authoritative |
| RunPod Serverless | provider-confirmed charge ID, separate from native estimates; known historical 0.9009412648156285 USD is not imported | Unsupported until reviewed adapter gate |
| Ads mediagen central/local ledger | preserve provider-operation ID, frozen lineage and central reservation/dispatch/reconcile flow; MediaSpend is reporting projection only | Follow-on Ads owner; no deployment/enrollment/live adoption |
| Gemini Developer API images | exact model/operation, usage, price/version/evidence; billed unknown until provider evidence | Follow-on; NOT Google AI Pro/Ultra app/Flow/UI allowances |
| Antigravity CLI | separate subscription funding category from Gemini media API | Unsupported; no configuration changes |
| Google promo/Cloud credits | credits require verified eligibility and actual application | Unsupported; never auto-deduct estimates |
| Sume / MiniMax / HeyGen / Grok | fractional credits and reference surcharge; separate MiniMax $5 and HeyGen $5 caps; Grok subscription-only, no metered fallback | Follow-on; H3Max blocked capture ceiling, no pooled caps |
| Meta delivery | one canonical nonoverlapping ad-level OR daily source by account currency/report timezone | Unsupported; nested campaign/adset/ad summaries must not all bill; budgets not charges, ROAS not revenue |
| Ryan/slopbox proxy | external_shared / billing_unknown | No demonstrated paid MiniMax calls; inactive health-only proxy |
| Direct PilotA scripts | bypass mediagen kit imports | Explicit coverage gap |
| Fireworks video / legacy image | no verified video route/rate; deprecated endpoints are not proof of no historical spend | Unsupported |

RunPod lifecycle observations may record reservation/release, create
accepted/uncertain/rejected, running/stopped/deleted, storage liability, revised
estimate and posted/reconciled charge. Stock polls and rejected zero-create are not
rentals. Stopped is not deleted; storage liability remains separate. Estimate only
rate × bounded observed lifetime; missing rates/times stay unknown. A posted charge
supersedes an estimate only when the caller supplies the same exact resource,
component and bounded period; a charge ID alone does not establish that match.
Cleanup is not invoice reconciliation. Uncertain
creates retain liability; no replay or inferred no-charge after cancellation/error.
PR320 ratebudget limits REST requests, not financial exposure. Native database
history is incomplete after authorized deletion/export; summaries are not charges.

Ads authority: Storytime#120 and merged server121/122/123; ergo-bots239 adapter
46e7534 does not authorize deployment or import. Source evidence is
`out/media-spend.jsonl`; relevant adapter files are
`ads/creative-kit/bin/mediagen/{central,cli,batch,ledger,history}.py`, `pricing.yaml`,
and `adapters/{sume,minimax}.py`. Gemini async create idempotency and billed-item
identity remain unproven. Ambiguous submissions retain exposure with no resend or
fallback. Financial source summaries must link to an existing operation, not create
another bill. Current invoice coverage is unknown.

## Migration and rollout

Remote main and lifecycle PR318 were inspected before implementation: PR318 merged
`0045_lifecycle_event`; this slice starts at additive `0046`. It does not alter
InferenceRequest or RunPod models/behavior. Review PR and green isolated CI are
required; this worker will not merge or touch production. API and dashboard owners
should use owner-scoped projection/read APIs rather than summing observation rows.

Constraint construction selects Django's supported keyword (`check` in 4.2,
`condition` from 5.1; 6.0 removed `check`). The migration carries its own historical
constructor, so it does not import mutable application helpers. The ledger suite
is exercised offline on Django 4.2, 5.2 and 6.0; Django 6 application startup is
also smoke-checked.

## Adoption slice: projection, adapters and coverage (stacked)

The following projection and adapters remain distinct from existing spend authorities.
Automatic private inference accounting is opt-in; nothing is backfilled.

- `GET /api/costs/ledger?days=&bot=` (Ergonaut) returns `summarize_costs` for the
  authenticated user and registered bots they may use (superusers see every owner),
  UTC calendar-day window, optional bot filter. Ordinary users get 404 for an
  inaccessible or unregistered requested bot; unfiltered results omit both. The Costs
  page shows it separately from the token estimate; the two are never added.
  `known_amount` is Decimal text, `null` when every row is unknown.
- Window semantics: ranking and ownership run first, then the window applies to the
  *selected* row: a bounded resource period overlaps the window (amount not
  prorated, counted in `partial_period_count`), otherwise `occurred_at`. Rows with
  no time are `undated_count`, never assigned to a window. A settled row outside
  the window therefore cannot be replaced by an in-window estimate.
- `django_ergo.costs.adapters.record_inference_request(actor, request)`: one
  operation per `InferenceRequest` id; `reserved_usd` is a reservation, a
  `priced` `cost_usd` is a list-price estimate (never final), and a finished
  request without both is `unknown`. A `not_sent` request also records a release;
  non-`api` billing is skipped (not evidence of free). Cache-read tokens remain
  nullable; uncached input is unknown because prompt includes cache writes that
  the source does not retain separately. Missing completion times remain undated.
  Owner, session and structured call ownership are validated, and all observations
  from one invocation are committed atomically. A local failure never warrants
  retrying the external request.
- `django_ergo.plugins.cost_runpod` (`record_job_reservation`, `record_component`,
  `estimate_amount`): reads only the durable `RunpodJob` fields; the caller supplies
  provider evidence. Compute and storage use distinct exact half-open periods
  keyed by provider/account/resource/component/start/end, at microsecond precision;
  a successor job is recorded as observation `attempt_ref`, not economic identity.
  The optional hourly estimate is rounded half-even to 18 decimal places.
  Overlapping nonidentical periods are rejected rather than prorated or inferred.
  Provider charge IDs are unique to one component/period within an account;
  provider-reported/final requires explicit amount and charge ID. Revisions for the
  same kind must be coordinated across attempts. Account-row locking serializes these
  checks among callers of this adapter; bypassing it invalidates that guarantee.
  Conflicting owner/bot attribution is rejected, not reassigned. Stop, delete,
  cleanup and the REST request budget are not money evidence. Does not touch
  lifecycle, budget or `native.py`.
- `django_ergo.costs.coverage.coverage_manifest()` reports the private inference
  hook's current configured state; all coverage is incomplete.

### Private inference opt-in and recovery

Set `DJANGO_ERGO["INFERENCE_COST_LEDGER_ENABLED"] = True` to enroll new
private-pool `InferenceRequest` attempts. The default is false. This covers only
the existing private inference attempt lifecycle, not other API engines, native
CLI, subscriptions or arbitrary bot tools. Enrolled attempts remain enrolled if
the flag is disabled while in flight. No existing rows are backfilled.

Each enrolled admission/outcome transition commits a durable pending marker with
the source row. After commit, the hook writes observations under a row lock and
clears the marker atomically. Accounting exceptions log only the request UUID and
exception class, leave the marker pending, and never cause provider dispatch or
replay. A process crash may also leave pending work; monitor pending rows and logs.
Run `python manage.py reconcile_inference_costs --batch-size 100` to retry a
bounded batch of accounting only, or `--request-id UUID` for a specific pending
row. A nonzero exit indicates failures; repair attribution or source data before
retrying. Permanently failing oldest rows can occupy a batch, so inspect them
and target later pending UUIDs explicitly. This is operator-driven, not scheduled.
Both pending and recorded states are local accounting state, not billing proof.

Remaining gaps include other API engines, RunPod lifecycle, rental `Experiment`
mapping, native CLI per-request keys, Ads/MediaSpend, Gemini, MiniMax/Sume/HeyGen/Grok,
and Meta delivery.
