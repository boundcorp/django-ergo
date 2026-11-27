# Paid Codex proxy: draft implementation and gated smoke runbook

**Real paid route: REFUSED. No paid calls, deployment, network policy, credential,
funding or shared infrastructure changes were made.** A production sandbox
runtime is absent. `PaidCodexPlugin.require_runtime()` always refuses, before a
capability or child process exists; setting `PAID_AGENT_SANDBOX_BACKEND`, a static
verification flag, or returning an argv prefix cannot open this gate. Replacing
this gate requires a separately reviewed implementation with launch-time evidence
from the same retained sandbox that will execute Codex and its descendants.
Scrubbed environment, HOME, argv and CLI sandbox options are not confinement.

Lee approved **$1 total OpenAI smoke within $25/week on November 27 at 20:08**.
That authorization persists. Execution remains gated on confinement, actual
weekly consent/account mapping, tariff and deployed service verification; no
request for renewed smoke approval is implied. Infrastructure, credentials,
funding changes and deployment require their own approval and are outside this
worker's scope. This is a draft branch, not a merge/deploy instruction.

## What the code enforces

The proxy is a separate threaded WSGI endpoint,
`django_ergo.conversation.paid_agent_wsgi:application`, not part of the normal
Ergonaut ASGI service. It accepts only text Responses or Chat Completions for
the captured OpenAI model. It revalidates current authority, privacy, provider,
account identity, prices, pause and key fences before every reservation. Restricted
coding privacy refuses even when a policy would permit the same chat route.
Hosted tools, hidden/stored context, remote modalities, endpoint/key overrides,
service tiers and unbounded fields refuse before upstream bytes. Local tools and
schemas count in the bound. `tool_choice` accepts local function/custom selectors
or `auto`, `none`, `required` only. The complete body plus framing and the injected
output ceiling bound worst-case spend. httpx ignores environment proxies, follows
no redirects, retries nothing and sends only to the fixed OpenAI API origin.

Capabilities are short-lived, launch-specific digests bound to user, grant,
account mapping, model, expiry, attempt count and SUM of worst-case reservations.
Cheap settlements do not replenish the launch ceiling. Token issuance releases
the unsent coding allocation atomically; only attempts become paid estimates.
A refusal at the absent-runtime gate releases a matching owned unsent allocation;
it cannot release a launched or in-flight attempt. Stop, exit, watcher and expiry
revoke capabilities without forgiving unknown liability.

Complete, validated authoritative terminal usage settles a Responses result
with `status=completed`, or `status=incomplete` and exactly
`incomplete_details={"reason":"max_output_tokens"}`. This includes the bounded
128-token smoke. A truncated output is not itself an unknown charge. Missing,
partial, bool, negative, inconsistent or implausibly zero input usage retains the
hold. Wrong model/tier, cost overrun, duplicate terminal data, mismatched SSE
status/event, events after terminal, disconnected streams and content-filter or
failed results remain unknown and revoke the capability. Partial token evidence
and provider request id are retained without inventing zero spend.

## Durable ledger retry

Every new `api`/`metered` source transition commits `cost_ledger_state=pending`
with the source row. The flag `INFERENCE_COST_LEDGER_ENABLED=False` cannot suppress
it. An on-commit attempt ingests immediately, and Ergonaut Celery beat schedules
`ergonaut.reconcile_inference_costs` every 60 seconds, 100 rows per sweep. This
retries accounting only; no inference is replayed. Failed rows rotate behind older
pending work, preventing a poison row from permanently starving a bounded batch.
The source row lock and ledger source identities make duplicate/replayed sweeps
idempotent. If the ledger or broker is down, the DB marker survives and source
reservations/spend still consume authority. Without a running beat/worker, use
`reconcile_inference_costs --batch-size 100`; live scheduler rollout is unverified.

The cost API's `coverage.pending_inference_attempts` reports pending ingestion
scoped to the authenticated owner and accessible bots. The coverage manifest
exposes the same count. Reported ledger totals with a nonzero count are incomplete;
they do not mean zero spend. Historical unmarked rows are not silently backfilled.
`ergonaut.expire_paid_agent_launches` is also scheduled every 60 seconds; it revokes
expired capabilities and converts abandoned pending attempts to unknown holds.
Neither maintenance task has been deployed by this work.

## Merged #407 integration

The base is actual fetched main `64fbee1`, including merged #407 at `c0d63aa`
and #414 analysis-runtime fixes. The #407 migration is
`0065_provider_weekly_budgets`, after `0064_isolated_analysis`; this PR is therefore
**0066_paid_agent_proxy**, a single linear graph. The rebase settlement conflict
was resolved by retaining #407's provider/envelope locks and invoking the durable
ledger transition inside them. No #407 checkout was edited.

`ProviderAccountMapping` is the single audited identity source. There is no
`PAID_AGENT_OPENAI_ACCOUNTS` settings map. The launch/ledger `account_ref` is the
immutable mapping row id; the human org/project/key-id assertion belongs in
#407's audited `account_label`/`assertion`. A mapping to another family refuses.
Proxy attempts inherit `account_mapping`, `provider_family` and `budget_week`
inside the same #407 reservation transaction used by chat and other API work.
The launch stores the allocation's week and refuses new attempts after rollover;
an in-flight Sunday attempt can settle Monday without moving its week. Unknown
holds carry into every later week's availability until evidenced reconciliation.

The ordinary recurring coding-launch refusal remains. The reserve seam permits
only a proxy-bound OpenAI coding authority with a captured real `Decision`, route,
identity and current funding; it still enforces per-launch, pool, owner and shared
provider weekly caps. Selection can reserve an allocation, but it cannot authorize
execution: the runtime gate remains closed. Lee's $25/week must be represented by
actual audited consent and funding in the deployed DB; synthetic tests do not
assert the live state. The smoke's $1 is a child ceiling within that authority.

## Local external confinement harness

Run with real provider keys unset and `ERGO_TEST_ISOLATED=1 TEST_OPENAI=false`:

```
.venv/bin/pytest tests/test_paid_agent_confinement.py -q
```

`test_external_namespace_harness_denies_key_and_direct_egress` launches a real
bubblewrap user/PID/mount/network namespace with dropped capabilities and only
runtime libraries, fresh proc/dev/tmp, a probe and one mock proxy socket mounted.
An external same-UID sentinel holds a **fake** key; the parent namespace also
contains a fake credential file and a mock TCP provider listener. Inside the
sandbox and in its descendant, key/proc and host-file access is denied, direct
TCP access to the host listener and synthetic external IP is denied, and the
external mock proxy returns HTTP 200 through the sole mounted Unix socket.
The review's ordinary same-UID `env` reproducer is retained as a negative control
and still demonstrates the leak without isolation. Claimed backends and static
flags are tested to refuse production launches.

This proves local kernel boundaries and external mock proxy access only. It does
**not** prove Octo NetworkPolicy enforcement, DNS/IP denial for api.openai.com in
a deployed pod, cluster-private TLS, a deployed TCP proxy Service, or race-free
sandbox lifecycle/attestation. It is deliberately not registered as a production
backend. Octo ordinary Ergonaut pods still have ambient provider keys and no
proven provider-denying boundary; never run paid Codex in those pods.

## Minimal proposed infrastructure scope (not changed)

The authoritative map is `~/ENVIRONMENT.md`; Octo uses explicit `--context octo`.
`/home/dev/bin/envcheck` succeeded (both clusters ready, documented host routes
reachable). The independent review establishes ordinary Ergonaut pods use
`envFrom: ergonaut-runtime` and have unrestricted provider egress; this draft
makes no claim to have corrected live confinement.

The inspected `boundcorp/infra-octo` layout has
`kubernetes/apps/kustomization.yaml`, `kubernetes/apps/ergonaut/ks.yaml` and
`kubernetes/apps/ergonaut/app/{deployment.yaml,kustomization.yaml,rbac.yaml,service.yaml,runtime-secret.sops.yaml}`.
Propose a separate app, confined to new namespaces and labels:

| Proposed path in infra-octo | Minimal reviewed change |
| --- | --- |
| `kubernetes/apps/kustomization.yaml` | Register only the new paid-agent app. |
| `kubernetes/apps/paid-agent/kustomization.yaml`, `ks.yaml` | Flux reconciliation for the new isolated app. |
| `kubernetes/apps/paid-agent/app/kustomization.yaml`, `namespaces.yaml` | New `paid-agent-sandbox` and `paid-agent-proxy` namespaces; no global namespace policy edits. |
| `kubernetes/apps/paid-agent/app/sandbox-template.yaml` | Dedicated unprivileged keyless Job/pod, empty workspace, no `ergonaut-runtime`, host home, hostPath, Docker socket, shared PID namespace or service-account token; read-only root, dropped capabilities, reviewed seccomp; no hostNetwork/hostPID. |
| `kubernetes/apps/paid-agent/app/sandbox-networkpolicy.yaml` | Select only new sandbox pods; deny all ingress/egress by default, allow DNS only to selected cluster DNS and HTTPS only to the dedicated proxy pod/port. Verify actual CNI enforcement for IPv4/IPv6, direct IP, DNS aliases, metadata and descendants. |
| `kubernetes/apps/paid-agent/app/proxy-deployment.yaml`, `proxy-service.yaml`, `proxy-networkpolicy.yaml` | Separate threaded WSGI proxy, cluster-private TLS, only sandbox ingress; DB/registry/config reads and reviewed provider egress only. No Authorization/body logging. No public ingress. |
| `kubernetes/apps/paid-agent/app/proxy-secret.sops.yaml`, `proxy-configmap.yaml` | Separately approved proxy-only credentials/config; never copy the broad runtime secret into the sandbox. No secret values in proposals/tests. |
| `kubernetes/apps/paid-agent/app/controller-rbac.yaml` | Namespaced launch controller with minimum Job/get/log/delete rights in new sandbox namespace, no Secret read and no global privileges; controller separated from provider credentials. |

Before replacing `require_runtime`, implement a controller/client that creates a
specific sandbox, validates its immutable workload/policy identity, and runs
challenge-bound launch-time probes **inside that same running sandbox** before
issuing a capability. Verify key/env/proc/mount/login/socket invisibility, denied
provider/DNS/direct-IP/metadata egress and successful TLS proxy reachability from
the child and descendants. The external controller must verify outcomes and
bind evidence to pod UID, policy/image digest, nonce, proxy identity and expiry;
a backend's own boolean report cannot suffice. Keep the same boundary alive
through exec (no prepare-in-one/launch-in-another gap); failure/timeout kills the
sandbox and refuses launch. No production implementation exists in this PR.

Ordinary Kubernetes NetworkPolicy does not express an API hostname allowlist.
The proxy egress design needs a reviewed gateway or maintained IP policy; do not
assume an `api.openai.com` string enforces it. Any new gateway is a separate
approved infrastructure change. Sandbox DNS is limited to cluster DNS; provider
TCP must fail for both domain-resolved and literal IP targets, and external DNS
and metadata paths must fail as well.

These paths are a proposal, not manifests ready to apply. Lee/infra owners must
approve new workloads, namespace-scoped policies, RBAC, service/TLS, credential
provisioning and reviewed deployment/rollback. Do not edit shared pods, global
network policies or `ergonaut-runtime` as a workaround. Ensuring the selected
credential is exclusive to the proxy may require a separately approved dedicated
credential; this task does not create, move, rotate or remove any key. Deploy and
verify the matching code, migration and beat tasks only after review and the
separate load-bearing rollout authorization. The previously approved smoke is
still blocked until all those facts are evidenced.

## Smoke checklist — NOT RUN

1. Complete the runtime/controller and isolated infrastructure review, approve
   its deployment, and verify actual in-sandbox denials plus proxy reachability.
2. Verify the deployed commit/migration and maintenance schedules, current exact
   standard-tier model tariff, proxy-only key/account and one audited #407 mapping.
3. Inspect actual Lee consent/funding for $25/week UTC, remaining authority after
   all spend and unknown holds, and a $1 **total** child ceiling. Use at most three
   attempts, five-minute TTL and a bounded 128-token output limit. No extra funding.
4. Run a single empty-workspace hello task only after these gates pass. Observe
   authoritative usage even when output is truncated; stop on model/account or
   billing discrepancy and retain unknown exposure. Do not issue paid calls from
   this worker's mock-only scope.
5. Verify exit revocation, rejected token reuse with zero upstream sends, one
   source row and one monetary ledger projection per attempt, and allocation
   release separately. Preserve redacted dispatch/request ids and weekly mapping.
6. Reconcile with authorized provider evidence below. Treat local estimates as
   **UNVERIFIED** charges until that evidence is retained and reviewed.

## OpenAI evidence access required

Use an **API Platform organization Admin API key**, held only by the accounting
operator, with authorization for read-only usage and costs in the selected API
organization. A normal project inference key and a ChatGPT workspace Admin key
are not substitutes. Only an API Platform organization owner can create the
organization Admin key; provision/access/roles were not tested or changed here.
See [Admin APIs](https://developers.openai.com/api/docs/guides/admin-apis) and
[Admin-key ownership](https://help.openai.com/en/articles/9687866-admin-and-audit-logs-api-for-the-api-platform).

Query `GET /v1/organization/usage/completions` over the recorded UTC interval,
filter `project_ids` and `api_key_ids`, group by project/key/model/service tier,
use `bucket_width=1m` where appropriate, and follow every pagination cursor.
This is aggregated token/request evidence; it is **not a per-request-ID billing
API**. See the [Usage API reference](https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage/methods/completions).

Query `GET /v1/organization/costs` for the covering UTC days, filter the project
and key where supported and group by project/key/line item. It currently offers
daily buckets, so unrelated activity, delayed reporting and price adjustments
can prevent isolation of a $1 smoke from one export. Retain query bounds,
organization/project/key IDs, retrieval time, all pages and currency. The
[Costs API reference](https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage/methods/costs)
documents these filters. For an exact request-ID cross-check, obtain authorized
provider request logs/support evidence; do not invent a request-level Costs
endpoint or infer a zero charge from absent/delayed buckets. Organization/project
Usage Dashboard export with authorized visibility can supply additional
provider evidence, with its aggregation limits recorded. No provider billing
permission or charge has been verified by this work.
