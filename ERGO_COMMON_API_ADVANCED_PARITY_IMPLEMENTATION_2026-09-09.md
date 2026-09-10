# Ergo common public KB API: advanced parity implementation

Date: 2026-09-09 · Primary task: `01a08756-6fcd-7763-8ec0-09bd9f974dbf`
Worktree: `/home/dev/orca/workspaces/django-ergo/codex-ergo-direction-review`

**This implements the clarified requirement, not just preservation of old DB
features.** Hierarchy/strategy, semantic and weighted/hybrid retrieval, and
usage tracking now exist on CorpusService for memory, database and committed
Git corpora. Existing Article callers retain their original APIs and have an
explicit reviewed import/publication compatibility bridge.

Implementation is uncommitted and undeployed. Final verification is complete;
the final counts/status are recorded below. Earlier reports are
marked as historical milestones, not current completion claims.

## Gap-to-implementation mapping

| Verified gap in the preceding milestone | Implemented common public API and evidence |
| --- | --- |
| Document had no summary or placement; TOC only listed IDs/titles | Document.summary/hierarchy_code; table_of_contents(prefix), by_hierarchy_prefix, get_by_hierarchy, create_page with explicit/parent/section placement; revise for moves/summary changes. Shared tests cover all three readers and writable staging. |
| Organization strategy existed only on legacy Knowledgebase | Versioned reviewed strategy document; get_strategy, propose_strategy, propose_tree, get_tree_status. Same approval/CAS/history contract as pages, active-only tree counts. |
| New service only exposed lexical search | semantic_search_content/summary, multi_field_semantic_search, vector_search_content/summary, multi_field_vector_search, hybrid_search; search(mode, weights) dispatch. Same optional provider/index for all storage adapters. |
| Usage only had Article/Knowledgebase FKs or host-only audit suggestions | record_usage, with_usage_context, usage; automatic operation observations; MemoryUsageStore, DatabaseUsageStore and FileUsageStore. Sink choice is independent of KB storage. |
| CorpusToolkit skipped conversation usage recording | Existing runner invokes a logical usage hook; session-bound service clone prevents context bleed. Old ConversationKBUsage FK recording remains unchanged for Article toolkits. |
| No explicit bridge for existing Article authoring/tool callers | ArticleCompatibility.propose_import creates reviewable evidence/pages/layout/strategy; publish transactionally upserts accepted heads, preserving UUIDs and legacy field/provider behavior. Source fingerprints/row locks reject concurrent legacy edits. |

None of these requested features is designated “future work” or restricted to
Article APIs. Compatibility does not mean replacing legacy QuerySet return
types with JSON dictionaries in existing callers: the new public API returns
structured cited records while old APIs keep their established types.

## Concrete implementation

- `src/django_ergo/knowledge/schema.py`: canonical v1/v2 logical schema.
  V2 adds summary/hierarchy and strategy; old empty fields are omitted, retaining
  original v1 content/corpus hashes and review receipts. Golden hashes were
  obtained from the prior installed v1 wheel and added as regression evidence.
- `src/django_ergo/knowledge/hierarchy.py`: placement allocation, tree Markdown
  construction and active-page gap counts. Layout codes are logical strings,
  never filesystem paths. Duplicate head codes, including archived reservations,
  fail. Moves create new reviewed revisions.
- `src/django_ergo/knowledge/retrieval.py`: MemoryVectorIndex, exact cosine
  scoring, field weights and explicit CapabilityUnavailableError. Collection,
  scope, corpus revision, dimension and provider fingerprint bind the projection.
- `src/django_ergo/knowledge/service.py`: shared authorization, advanced public
  methods, active-page re-binding, lifecycle exclusions and usage observations.
  Index/query providers are invoked only after explicit host permissions.
- `src/django_ergo/knowledge/usage.py` and `usage_files.py`: scoped/idempotent
  event storage; optional DB persistence or POSIX locked/fsynced JSONL.
  No source text or query text is copied into usage records.
- `src/django_ergo/knowledge/toolkit.py`: fifteen provider-adapter-compatible
  tools, including semantic/hybrid search and proposed layout/strategy changes.
  No agent-facing approval/apply tool is added.
- `src/django_ergo/conversation/runner.py`: additive logical-usage hook without
  changing old Article FK usage tracking.
- `src/django_ergo/knowledge/articles.py`: explicit host-authorized Article
  import/publication compatibility, with no invented review approvals.
- `src/django_ergo/knowledge/management/commands/ergo_corpus.py`: advanced
  search modes/weights, hierarchy/strategy/usage actions, and explicit
  --rebuild-index for same-process CLI use.
- `src/django_ergo/knowledge/migrations/0003_corpus_usage.py`: independent
  additive usage table. Applied legacy migrations are unchanged.

Host integration and executable examples are documented in
`docs/knowledge-foundation.md`; independent project-membership and
company/site-membership policies remain in `examples/knowledge_hosts.py`.

## Retrieval and provider contract

The provider shape matches existing Ergo embedding providers:
generate_embedding(text), get_dimensions(). The host supplies a stable
model/version/configuration fingerprint; no SDK/global provider is selected
because a KB happens to be filesystem- or database-backed.

The default lexical mode remains useful without providers or indexes.
Semantic methods require a configured/rebuilt index; text queries also require
a provider, while precomputed vector queries do not. Missing/unbuilt/stale
capabilities raise explicit errors, never a disguised lexical fallback.

Weights support content, summary and title. Empty selected fields are omitted
and remaining weights normalized per document; all-missing documents are
excluded. Vectors and weights must be finite and dimensionally valid.
Hybrid is actual lexical/cosine fusion, unlike the legacy Article hybrid alias.
The old alias's behavior is preserved for old callers.

Indexing is explicit and only includes authorized, reviewed active page heads.
Archive or other corpus changes invalidate the old projection; rebuilding
cannot resurrect archived pages. The supplied index is a process-local exact
projection, not persistent ANN infrastructure. A host can supply the same
document/revision-bound index interface with different persistence/performance.

## Persistence and compatibility semantics

**Git:** GitCorpus remains a committed reader, not a covert writer. Author in a
reviewed MemoryCorpus/DatabaseCorpus workspace, then explicitly export/write/
commit through the host publisher and select its trusted ref. Tests verify
that staged apply does not alter Git, then publish a synthetic v2 fixture and
read its strategy and semantic results through the committed adapter.
No original/user repository was published.

**Usage:** the default sink is process-local MemoryUsageStore for *every*
backend. Durable history requires selecting DatabaseUsageStore, FileUsageStore,
or a host sink. The tests exercise the full three-corpus × three-sink matrix
under both host policies. File journals are independent of Git publication,
use POSIX locking and new-file mode 0600, and require explicit host rotation at
16 MiB. Usage failure is surfaced; UsageRecordingError.committed identifies a
failure after canonical publication, so callers inspect operations before retry.

**Article bridge:** its separate authorization callback grants export of legacy
rows (including archived records) or publication into the legacy KB. A corpus
scope grant does not authorize a different source. Import is pending until
normal host review/apply. Publish preserves existing UUIDs; non-UUID logical
IDs map deterministically into the bound KB namespace. It rejects foreign-ID
and hierarchy conflicts and stale source fingerprints under row locks.
Missing destination Articles are preserved, not deleted. This is explicit
copy/import, not automatic bidirectional reconciliation of deletions; use the
common reviewed withdrawal API for intentional removals from retrieval.
Clearing a legacy strategy is represented by a reviewed common strategy update.

**Installation/upgrades:** minimal Django/django-environ packaging remains.
Legacy hosts select django-ergo[legacy]; vector/OpenAI/YAML imports do not enter
the common contract. New common DB usage needs only the lightweight app's
migration. Existing Article PostgreSQL/vector migrations and APIs remain intact.
This is not a conversion of legacy databases to SQLite.

## Test evidence

- Full isolated pgserver suite: **718 passed, 20 skipped**, 200.78 seconds.
- Provider-free memory/SQLite/shared-adapter suite: **281 passed**, 55.97 seconds.
- Earlier focused Article/toolkit compatibility run: **102 passed**; the final
  full run additionally covers source-fingerprint and strategy-clearing cases.
- New knowledge modules, host examples and new tests pass Ruff; formatting and
  git diff whitespace checks pass. No repository-wide baseline lint claim.
- `makemigrations ergo_knowledge --check --dry-run` reports no changes; previously
  applied legacy migration files have no diff.
- Latest wheel built locally at
  `/tmp/ergo-direction-wheel/django_ergo-0.1.0-py3-none-any.whl`; installed only into
  `/tmp/ergo-direction-minimal`. The imported module path was verified inside
  that virtualenv, not the source checkout. Installed distributions are only
  Django, django-environ, asgiref, sqlparse and django-ergo.
- That installed wheel passes the advanced memory/SQLite smoke flow: reviewed
  intake/placement/strategy, content/summary weighted and hybrid retrieval,
  hierarchy, citations, operation history and usage. No pgvector, psycopg,
  OpenAI or YAML package is installed there.

Evidence entrypoints:
- `tests/test_knowledge_features.py`: cross-backend advanced methods, hierarchy
  and strategy writes, weighted/hybrid ranking, scope/denied access, lifecycle
  exclusion, exact citations, missing/stale capabilities, invalid vectors/
  weights, usage sink matrix, JSONL reopening, context isolation, explicit Git
  publication, tool/command integration, post-commit usage failure and v1 hashes.
- `tests/test_knowledge_article_compatibility.py`: Article import/correct/publish,
  old caller identity/embedding preservation, strategy clearing, concurrent
  legacy edits, denied source access and actual runner usage binding.
- `tests/test_knowledge_boundary.py`: optional imports blocked during new-app
  startup and memory/SQLite writes, hierarchy/strategy, semantic/weighted/hybrid
  retrieval and usage using an explicit tiny provider. Same script is run from
  the installed minimal wheel, not merely the source checkout.
- Existing ingestion/memory/write/review/toolkit suites remain in the full run.
  Passing tests are evidence for the implemented mapping above, not a substitute
  for inventory or a claim of live provider/production validation.

Exact provider-free invocation:
```bash
PYTHONDONTWRITEBYTECODE=1 /home/dev/p/boundcorp/cabal/.venv/bin/python -m pytest \
  tests/test_knowledge_corpus.py tests/test_knowledge_writes.py \
  tests/test_knowledge_features.py tests/test_knowledge_boundary.py \
  --ds=tests.knowledge_settings -o addopts='' -q --tb=short
```

Exact full invocation (disposable pgserver; no shared DB or real provider keys):
```bash
env -u DATABASE_URL -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u TEST_OPENAI \
  ERGO_TEST_ISOLATED=1 PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH=/tmp/ergo-direction-review-test-deps:src \
  /home/dev/p/boundcorp/cabal/.venv/bin/python -m pytest tests -q --tb=short \
  -o addopts='' --reuse-db --ds=tests.example_app.settings
```

## Exact remaining boundaries

The requested advanced parity is implemented. These are explicit integration/
scale boundaries, not promises to add the requested functions later:

- Hosts still issue review receipts, authorize providers/source admission,
  choose users/scopes, persist pending/rejected artifacts, and run jobs/UI.
- Memory indexes/usage are volatile unless the host chooses another sink/index;
  no automatic charged rebuild, graph, ANN service or voice orchestration.
- Corpus snapshots retain full revision history within existing 10,000-record,
  1 MiB/content-field and 16 MiB/corpus bounds. This is a small-corpus foundation,
  not an incremental large-corpus storage/index design.
- Summary text is authored/admitted content; common indexing embeds it but does
  not implicitly invoke a summarization model. Existing Article field behavior
  still runs when the compatibility adapter publishes to Article.
- Article imports/exports and Git publication are explicit boundaries; no silent
  merges, deletion reconciliation or dual-writer synchronization.
- Live external providers and older advertised Python versions are not certified
  by deterministic tests on Python 3.12.11. No ready-to-deploy claim.

## Preservation and execution

The original direction memo and six held planning documents remain preserved.
Both Cabal worktrees and fs-vector remain untouched; no raw session/auth
material was copied. No agents/orchestrators, commits, merges, deployments,
production mutations or usage resets were used. Temporary fixture Git commits
and disposable test databases are not user-repository publication.

The original direction memo still has SHA-256
`1cb682c5a5ff19ca74f1ab53f063d83aa85b2d269e5c34a20b11b330fffdaec3`.
The six original planning-document checksums were rechecked against the earlier
inventory; all match. June and May26 still lack verified backups; attachment
metadata is not treated as independently verified backup.

**Continuing status: the requested common-API advanced parity implementation
and validation are complete. No work, tests or worker processes are continuing.
No commit, merge or deployment. Primary task retains product/release decisions.**
