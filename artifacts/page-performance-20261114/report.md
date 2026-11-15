# Ergonaut page latency investigation — 2026-11-14

## Decision

Fix the verified session-query join fanout first, reuse repeated recipient-card status within each response, and remove quadratic frontend context placement/grouping work. Add request/DB/phase instrumentation to the existing metrics middleware, not a second telemetry system. PR only; no merge, deployment, production restart, migration, infrastructure/config/secret change, or devbox mutation.

Base: `origin/main` `bc91422133fc5dea5679c914fa69729a9ac60b9d`, fetched before creating branch `perf/ergonaut-page-latency` in a new worktree `ergonaut-page-latency-fix`. Other worktrees and PRs #341/#321 were not modified.

## Production evidence (before this PR)

All cluster operations used explicit context `octo`, namespace `ergonaut`; `envcheck` reported both cluster APIs ready. Production reads were bounded, sequential, and read-only. No production message/session detail GET was issued: that handler updates the owner's `read_at`. Existing admin-session credentials were used in memory for three GETs, never printed; no API key was created or used, avoiding its last-used write.

| Observation | Measurement | Interpretation / limit |
| --- | --- | --- |
| Authorized `GET /api/sessions` on one web pod via localhost | TTFB 8,618.66 ms; total 8,618.83 ms; 256,846 B; HTTP 200 | Server-side latency dominates this request; no ingress/TLS/mobile network included |
| Authorized `GET /api/bots` same route | TTFB 93.85 ms; total 95.00 ms; 4,078 B; HTTP 200 | Directory endpoint is not the measured multi-second bottleneck |
| Public home shell via HTTPS ingress | TTFB 136.97 ms; total 137.03 ms; 819 B; HTTP 200 | Shell only, not authenticated page usability or JS render |
| Unauthenticated sessions via ingress | TTFB 120.51 ms; HTTP 401 | Auth rejection works; not page-speed evidence |
| Existing process-local session-list histogram | 7,516.89 s / 534 requests = 14.08 s cumulative mean | One process since restart; includes one 401; not a time-window rate, fleet mean or percentile |
| Existing process-local thread-detail histogram | 22.12 s / 10 requests = 2.21 s cumulative mean | Existing user traffic; not a new synthetic production request or percentile |
| Database activity, separate snapshots | 27–40 active session SELECTs; oldest 37–53 s; many `BufFileRead` / `BufFileWrite` waits | Query text is clipped to 1,023 bytes in pg_stat_activity; observed prefixes are session annotation SELECTs. Full-plan diagnosis comes from code/local execution, not fabricated production EXPLAIN |
| Bidirectional thread-message fanout | 211 sessions; max incoming 731, outgoing 391; max product 285,821 rows; summed products 1,018,062 | Read-only aggregate with 8 s statement timeout. Old count joins combine **all** related rows before filtered DISTINCT counts |
| DB cumulative temporary data | 98 TB; 2,747,943 temp files | Lifetime counter, not a rate or attribution exclusively to these requests |
| Resource snapshot | DB 2,565m CPU / 398 MiB; web replicas 149m/96m CPU, 215/206 MiB; bot-tasks 1,682m CPU / 626 MiB | Pressure is observable; snapshot does not prove absence of CPU contention. Web memory limit 1 GiB, CPU request 150m, no CPU limit shown |
| Bounded logs, final check | Last 1,500 lines per pod, within 15 min: 1,496 / 1,494 HTTP 200, one 401, one 404; no 5xx or Traceback/ERROR/TimeoutError/OperationalError markers | Truncated sample, not complete production history. Includes 235/188 list requests; not merely scrape health |

The existing production metrics endpoint is enabled and admin-cookie readable (HTTP 200, 61,747 B); `TELEMETRY_METRICS_TOKEN` is unset. The initial `/metrics` request hit the SPA shell because the actual endpoint is `/metrics/`; the configured endpoint correctly rejected an unauthenticated request. This explains why an authenticated in-process observation is possible but does **not** establish sustainable Prometheus scraping.

Database reads also emitted an existing PostgreSQL collation-version mismatch warning (database 2.28, runtime 2.34). Reported for the database owner; no repair, rebuild, config or schema change attempted.

## Verified causes and targeted changes

1. `api/bots.py::with_open_counts` joined both incoming and outgoing ThreadMessage relations, grouped their product, and used two filtered `COUNT(DISTINCT ...)` expressions. Correlated session-status annotations were evaluated through that enlarged query. Replaced with independent, FK-index-backed correlated count subqueries, coalesced to zero. Original status/sender/reply filters, visibility, ordering and 200-row navigation limit remain unchanged. This also helps thread-detail annotation and sent-card recipient annotation.
2. `sent_out` recomputed `thread_summary` for every sent request, even when all 30 cards targeted the same thread. Cache each recipient summary only inside the response; individual request IDs/status/text/reply/PRs remain distinct. No cross-request cache or stale real-time state.
3. Transcript Context placement scanned calls and filtered the full transcript per assistant. Precompute placement with sorted assistant boundaries and first-claim successor links, memoized by messages/calls. Preserves inclusive bounds, input-call priority, contextless overlaps, unfinished calls, older-page merges and final assistant placement. JSON worker-result decoding now happens only in worker-tool branches.
4. Sidebar grouping appended to fresh local arrays instead of repeatedly copying each growing array. Sorting, collapsed groups, caps and caller inputs are preserved.

No pagination/history rewrite, permission broadening, attachment rendering change, context-content removal, voice change, Markdown viewport change, blanket caching or asset code-splitting was made.

## Local before/after benchmark (NOT production improvement)

Same workstation, Python 3.12 / Django 5.2.19 / embedded PostgreSQL; three sequential samples per stage, median wall time. Each stage recreated the same synthetic data: two same-owner threads; 731 requests normal→long and 391 long→normal, alternating open/completed statuses; 2,000 versus 20 messages; one text block/message; one completed chat_reply per two messages with a 35 KB synthetic context section. Latest page remains 50 messages. No model/provider/media calls. Captured SQL execution time is Django's rounded debug query time; wall time includes ORM/conversion/accessory work. These are direct Python endpoint/helper calls, not authenticated HTTP requests.

| Local operation | Before median ms | Final median ms | Queries before → after | Response JSON bytes before → after |
| --- | ---: | ---: | --- | --- |
| Annotated session query | 4,130.09 | 21.98 | 1 → 1 | n/a |
| Sidebar list endpoint | 4,036.15 | 23.35 | 2 → 2 | n/a |
| Long thread, newest 50 / 2,000 messages | 3,582.12 | 213.95 | 260 → 28 | 45,118 → 45,118 |
| Normal thread, all 20 messages | 3,569.92 | 103.37 | 260 → 28 | 32,021 → 32,021 |

Intermediate independent-count fix alone: sidebar 21.62 ms, long thread 3,495.03 ms, normal thread 598.69 ms; thread queries still 260. The second bottleneck was therefore measured, not inferred from query count alone. Reusing repeated card status reduced it to 28 queries. Final long-thread SQL samples 135/141/142 ms; normal 39/41/39 ms; sidebar 10/8/8 ms. One SQL statement was **not** evidence of cheap work: the regression now checks actual executor row counts as well as semantic counts.

CPU-only frontend benchmark: Bun 1.4.2, 3 warmups, median of old=7/new=21 samples. Synthetic assistant/user messages and one call per four messages. No DOM, HTTP, React commit or production claim.

| Context placement case | Messages / calls | Old ms | New ms |
| --- | --- | ---: | ---: |
| Disjoint calls | 250 / 63 | 0.478 | 0.068 |
| Overlapping calls | 250 / 63 | 1.504 | 0.037 |
| Unfinished calls | 250 / 63 | 0.717 | 0.039 |
| Disjoint calls | 1,000 / 250 | 5.856 | 0.179 |
| Overlapping calls | 1,000 / 250 | 11.559 | 0.129 |
| Unfinished calls | 1,000 / 250 | 11.451 | 0.137 |
| Disjoint calls | 4,000 / 1,000 | 99.666 | 0.487 |
| Overlapping calls | 4,000 / 1,000 | 177.330 | 0.333 |
| Unfinished calls | 4,000 / 1,000 | 162.530 | 0.341 |

### Reproduction

Install the normal Ergonaut development environment (`cd ergonaut && make venv`) and migrate **only an isolated local database**. The repeatable behavior/workload boundaries are in `test_page_performance.py` and `contextPlacement.test.tsx`; no production credentials or data are required. To repeat the numeric endpoint benchmark, use the Python recipe in the appendix below on baseline `bc91422` and this PR, in separate isolated worktrees/databases; do not run it against production.

For frontend timing, use the compatibility oracle in `contextPlacement.test.tsx` as the old operation and exported `contextPlacement` as the new operation. Generate messages with `line = 0..N-1`, role `line % 4 ? 'assistant' : 'user'`; calls `ceil(N/4)` with starts `4*i` and ends `4*i+3` (disjoint), starts `0` and ends `N-1` (overlap), or starts `4*i` and null ends (unfinished). Use N=250/1000/4000 and the sample counts above, consuming returned map sizes. This recipe deliberately times placement only, not the browser.

## Actual UI / network proof

Ran real Chromium against an offline local Django ASGI server with fake bot/model clients and then the production-built Vite preview, not just unit tests. Signed in to the synthetic local account; inspected home/list, opened normal and long threads, loaded older history (50→100 visible messages, newest retained), expanded Context, navigated to `/threads`, and captured visual screenshots. No send/model action was triggered. Existing SSE request cancellation on navigation produced `net::ERR_ABORTED`; no application exception or observed API 5xx.

Production-built local long-thread navigation: visible newest message in 369.3 ms; detail HTTP TTFB 164 ms / total 165 ms / transferred 49,065 B. Server-Timing: total 152.54 ms, DB 63.37 ms, 30 queries (28 endpoint + 2 session/auth), lookup 30.286 ms, history 24.633 ms, call normalization 14.812 ms, accessories 59.475 ms. This is a single local smoke sample, not a before/after render benchmark or production/mobile SLA.

The development React StrictMode surface separately issued duplicate fetches and a 310 ms long task; production preview did not duplicate the initial detail request. Do not extrapolate dev-mode profiling to production. The production bundle is still 1,375.69 KB JS / 394.76 KB gzip and 85.44 KB CSS / 16.76 KB gzip; Vite's large-chunk warning remains. Asset/network splitting was not justified as the cause of the verified 8.6 s server TTFB and was not bundled into this fix.

## Instrumentation contract

Extends `PrometheusMetricsMiddleware` and the existing registry. Existing request counter/duration histogram remain, with bounded host `all`, allowlisted method (`OTHER` fallback), resolver route templates normalized to `{parameter}` and `unmatched` fallback; never raw paths/Host/IDs. Status counter label is a bounded HTTP code. Added histograms:

- `ergonaut_http_request_db_queries`: number of SQL executions.
- `ergonaut_http_request_db_duration_seconds`: aggregate execute time.
- `ergonaut_http_request_phase_duration_seconds`: one of four fixed phase names only.
- `ergonaut_http_response_size_bytes`: buffered bytes before compression.

Request-scoped `execute_wrapper` retains only a count/duration, never SQL or params. ContextVar phase scopes are inactive outside requests/when disabled. Existing synchronous Django middleware adaptation covers WSGI/ASGI sync/async views and thread-sensitive ORM bridges. Wrappers/context are removed after requests and exceptions. Streaming response bodies/SSE are not consumed or timed to completion. Server-Timing includes total/db/query-count/present phases and preserves existing entries.

Phases: session_lookup (permission/status), history (bounded message window), serialization (call normalization only; **not** framework JSON encoding), sidebar (navigation/accessory/status assembly). Framework/encoding/middleware costs remain in total. No message bodies, SQL, credentials, bot/session/user IDs or PII are added to telemetry.

## Grafana / Prometheus gap and safe integration proposal

Coordinator's Grafana Octo discovery found no Ergonaut dashboard and no obvious existing app latency/ingress-duration series; Kubernetes workload dashboards show resources, not page latency. This worker verified that app metrics already exist and are enabled, and that the missing production bearer token prevents a stable credentialed Prometheus scrape. Repository dashboard JSON exists under `ergonaut/docs/dashboards/`; its presence is not proof of import/scraping.

Deployment owner follow-up in **boundcorp/infra-octo**, not this PR: provision a dedicated scrape credential via the existing secret workflow, configure authenticated **per-pod** `/metrics/` scraping (not a load-balanced Service, because registries are process-local), and import/update an Ergonaut dashboard using the existing datasource. Do not persist a user's admin cookie as scrape auth. No GitOps/config/secret/dashboard changes were made here.

Useful dashboard panels once series are ingested: rate and 5xx by route/status; histogram p50/p95/p99 by route; aggregate DB time/query count; four phase durations; response bytes; existing web/DB CPU/memory and DB temp-write rate. PromQL examples are in `docs/ergonaut.md#request-performance`. Evaluate user-request latency/errors independently of scrape `up`; a healthy scrape does not prove user health. Existing dashboards filtering by host must switch to `host="all"` or omit that filter.

## Verification

- Focused backend: `pytest ergonaut/observability/test_middleware.py ergonaut/apps/bots/tests/test_page_performance.py ergonaut/apps/bots/tests/test_api.py ergonaut/apps/bots/tests/test_stream.py -q`: **91 passed**. Covers permissions, pagination/history/message links, attachments, live snapshots, bounded telemetry, WSGI/ASGI × sync/async DB capture, DB failures, stream non-consumption and cleanup, counts/filter semantics, actual plan work, list query scaling, repeated card state.
- Broader backend: `pytest ergonaut/apps/bots/tests ergonaut/observability/test_middleware.py -q`: **230 passed, 1 skipped**. Test teardown warned that one connection still used `test_postgres`; no test failed. Existing local static-assets, test secret, pytest-asyncio and Celery deprecation warnings remain.
- Frontend: `bun test src`: **369 passed**, zero failures. Existing React `act(...)` warnings remain. First integration run found four bad newly added test fixtures/count assumptions; fixed fixtures and removed incidental JSON.parse call-count assertions instead of re-pinning implementation behavior.
- `bun run build`: passed TypeScript and Vite production build; large-chunk warning noted above.
- Ruff checks/format checks: passed before broader backend verification.
- Independent read-only backend and frontend reviews: no actionable patch-introduced findings.
- Real browser smoke and sequential benchmark evidence above; no paid provider/model/media calls, production load/stress tests or production writes.


## Deployment, verification and rollback

Coordinator review and normal CI are required **before merge**. This worker does not merge. Normal main image CI/Flux-managed automatic deployment remains the existing path; no manual restart/reconcile/scale/config/migration action is required by this patch. No schema changes. Existing deployment init-container migration behavior is unchanged.

After an approved normal deployment, confirm the exact image revision, check real session-list and normal/long-thread navigation with a bounded authorized browser request, inspect Server-Timing and API 5xx/errors, and compare route/DB/phase histograms under ordinary traffic after sustainable scrape setup. This PR has **not** been deployed and does not claim production improvement.

Rollback: revert this application commit through a reviewed PR and the normal pipeline; no data/schema rollback or infrastructure change is needed. Timing can also be disabled by the existing telemetry flag through the deployment owner's approved workflow, but this worker did not change it. A rollback restores the expensive joins, so it is not a performance remedy.

## Remaining latency / boundaries

- Distinct recipient-card statuses still use existing per-thread queries (bounded to 30 cards), approval accessories can query per waiting recipient (20 requests), and PR worker/card paths may still perform per-item lookups. The response-local cache removes the measured repeated-recipient case, not every possible N+1.
- Loading historical pages can refetch attachments/usage/pins; desktop/mobile Pins can duplicate GETs. Ownership/invalidations need separate measured work, not a speculative rewrite in this PR.
- Metadata/context, whole-session attachment indexing, Markdown highlighting, transcript rerenders and the unsplit JS bundle can still matter for unusually large content or many loaded older pages. The new phase/DB/payload timings and reproducible placement tests provide visibility; no mobile/slow-network production client profile was performed.
- Existing database collation warning and sustainable telemetry credential/scrape/dashboard setup need their owners; neither was mutated.

## Appendix: isolated endpoint benchmark recipe

Run with `python manage.py shell -c "exec(open('/tmp/probe.py').read())"` after saving the code below to a temporary file in the isolated local environment. Remove that temporary file after collecting results. Use the same recipe on baseline and PR. No permanent benchmark service, provider credentials or production access is required.

```python
import json, statistics, tempfile, time
from pathlib import Path
from types import SimpleNamespace
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django_ergo.bots import webhooks
from django_ergo.conversation.models import (
    ConversationSession, ThreadMessage, SessionMessage, MessageBlock, StructuredCall,
)
from ergonaut.apps.bots.tests.fakes import fake_registry
from ergonaut.api.bots import with_open_counts, session_detail, list_sessions, SessionDetailOut

folder = Path(tempfile.mkdtemp()) / 'kitchen'
folder.mkdir()
(folder / 'bot.yaml').write_text('name: kitchen\nengine: {type: claude}\norchestration: true\n')
(folder / 'agents.md').write_text('Offline benchmark only.')
registry, _ = fake_registry(folder)
webhooks.set_registry(registry)
user, _ = get_user_model().objects.get_or_create(username='offline-latency-probe')
ConversationSession.objects.filter(user=user).delete()  # isolated synthetic account only
long = ConversationSession.objects.create(user=user, bot_name='kitchen', metadata={'bot_role': 'thread', 'title': 'Long offline thread'})
normal = ConversationSession.objects.create(user=user, bot_name='kitchen', metadata={'bot_role': 'thread', 'title': 'Normal offline thread'})
ThreadMessage.objects.bulk_create(
    [ThreadMessage(sender_session=normal, recipient_session=long, text='offline', status='answered' if n % 2 else 'waiting') for n in range(731)]
    + [ThreadMessage(sender_session=long, recipient_session=normal, text='offline', status='answered' if n % 2 else 'queued') for n in range(391)]
)
for session, count in [(long, 2000), (normal, 20)]:
    SessionMessage.objects.bulk_create([SessionMessage(session=session, sequence=i, role='assistant' if i % 2 else 'user') for i in range(count)])
    MessageBlock.objects.bulk_create([MessageBlock(message=m, sequence=0, block_type='text', text='Offline text ' + str(m.sequence)) for m in session.messages.all()])
    StructuredCall.objects.bulk_create([StructuredCall(session=session, user=user, kind='chat_reply', status='completed', first_sequence=i, last_sequence=i + 1, metadata={'context': {'sections': [{'name': 'history', 'tokens': 100, 'text': 'context ' * 5000}]}}, response={'type': 'message', 'text': 'offline', 'status': 'done'}) for i in range(0, count, 2)])
request = SimpleNamespace(auth=user)
operations = {
    'annotated_list': lambda: list(with_open_counts(ConversationSession.objects.filter(user=user).select_related('user')).order_by('-updated_at')[:200]),
    'sidebar_list': lambda: list_sessions(request),
    'long_thread': lambda: session_detail(request, str(long.id)),
    'normal_thread': lambda: session_detail(request, str(normal.id)),
}
for name, operation in operations.items():
    times, sql, queries, sizes = [], [], [], []
    for _ in range(3):
        with CaptureQueriesContext(connection) as captured:
            start = time.perf_counter()
            result = operation()
            times.append((time.perf_counter() - start) * 1000)
        sql.append(sum(float(q['time']) for q in captured.captured_queries) * 1000)
        queries.append(len(captured))
        sizes.append(len(SessionDetailOut(**result).model_dump_json()) if isinstance(result, dict) else None)
    print(json.dumps({'operation': name, 'median_ms': statistics.median(times), 'samples_ms': times, 'sql_ms': sql, 'queries': queries, 'bytes': sizes}))
```
