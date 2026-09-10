# Path-primary Ergo implementation — 2026-09-10

Primary task: `01a08756-6fcd-7763-8ec0-09bd9f974dbf`.
Destination: `leewardbound/codex-ergo-direction-review`.
Worktree: `/home/dev/orca/workspaces/django-ergo/codex-ergo-direction-review`.

## Outcome

Logical paths now organize the **common public KB API**, not just filesystem
Articles. The same paths, placement, navigation, reviewed moves, strategy trees,
search, citations and usage work with memory, Django JSON storage and committed
Git readers. A logical path requires neither files nor a Git repository.

This implements the user's changed direction and supersedes the code-primary
organization/uniqueness decision in the preserved September 9 reconciliation
report. It does not discard that implementation or merge the source checkout again.

Implementation commit: `4f620389c2351ea1f935224db88844f26aa0c366`.
Status: implementation and validation complete; not continuing. No push or deployment.
This handoff report is committed separately after the implementation.

## Implemented API and behavior

| Area | Implementation |
| --- | --- |
| Schema | Document.path; ergo-corpus/v3 for path records. V1/v2 remain readable with identical unchanged hashes/receipts. Empty paths explicitly represent unmapped compatibility data. |
| Lookup/navigation | get_by_path, by_path_prefix, navigation and path-primary table_of_contents. Prefixes match slash-segment boundaries. Directories are derived logical prefixes, not filesystem objects. |
| Placement | create_page(path=...) or create_page(parent_path=..., name=...). Hosts choose names; no filename/code inference or automatic semantic slug generation. |
| Moves | move/revise(path=...) append revisions; move_tree moves all matching heads including archived reservations in a single reviewed proposal. Stable IDs and historical citations survive. |
| Strategy | propose_tree/get_tree_status use explicit Path tree headings and path-based active-page counts. Subtree moves revise these headings in the same approval batch; arbitrary prose, links and historical evidence are not rewritten. |
| Toolkit/CLI | Six added path tools (21 total) plus path navigation, lookup, create/move/subtree/tree management-command actions. Host policy and exact review/apply receipts remain mandatory. |
| Article bridge | Existing relative_path imports, optional explicit source-ID-to-path mapping, code-free path publication, stable UUIDs and transactional path swaps. Foreign identities, untouched-row path collisions and managed source publication fail. |
| Legacy | Code metadata/read/allocation compatibility remains, but no common code-uniqueness invariant. Ambiguous common code lookup fails clearly. Old code trees require explicit legacy mode; unmapped content remains accessible by ID and cited search. |
| Existing features | Semantic/weighted/hybrid/lexical retrieval, evidence intake, review, lifecycle exclusion, exports, usage sinks and host integrations remain storage-independent. Moves stale derived indexes and require explicit rebuilding. |

Main files: `src/django_ergo/knowledge/paths.py`, `schema.py`, `service.py`,
`toolkit.py`, `articles.py`, `ingestion.py`, and
`knowledge/management/commands/ergo_corpus.py`. Article ordering, TOC, validation
and additive migration live in `src/django_ergo/models.py` and migrations/0014.
The legacy portable wiki reader adds the actual wiki-relative path as explicit
input; virtual parsed wiki records can supply the same metadata without YAML.

## Contract and compatibility boundaries

- Paths are case-sensitive NFC Unicode, relative, at most 1024 characters.
  Empty/dot/traversal segments, controls, leading/trailing segment whitespace,
  backslashes, colon, percent/query/fragment markers and backticks are rejected,
  not silently normalized. No physical file extension or parent directory is
  required. A logical page node may have children.
- Current-head paths are unique per collection/scope, including archived
  reservations. Other scopes/collections may reuse a path. Moving frees the old
  head path for intentional reuse; there is no redirect. Citations use stable
  IDs, revision labels and content digests, so old references do not follow a
  reassigned path.
- Blank path is an explicitly unmapped compatibility state, not a generated
  filename. Old pages remain in unfiltered TOC/ID/search and can be reviewed into
  chosen paths. They are not counted as members of path trees. Evidence and
  strategy records do not masquerade as page paths.
- Existing v1/v2 document/corpus hashes and proposal receipts are retained when
  records are unchanged. V2 golden hashes were obtained from the pre-path
  installed wheel, not guessed; tests also retain the earlier v1 goldens.
  Assigning or moving a path changes semantics, creates a new revision and
  requires fresh approval. Old readers cannot consume v3 safely; upgrade consumers
  before publishing v3. Path fields under older format declarations are rejected.
- Old code APIs are compatibility-only. Common get_by_hierarchy rejects multiple
  active matches instead of choosing one. Legacy ORM/tool consumers that assume
  one code must keep their codes unambiguous or switch to IDs/paths. Article's
  get_legacy_table_of_contents preserves the old view; its default TOC is now
  path-primary with an explicit unmapped-ID marker.
- GitCorpus still reads committed manifests. Document.path and content_path are
  different: the latter is an adapter content locator. Review/apply in memory or
  DB, export, then let a host-authorized publisher write/commit/select the ref.
  A logical move never secretly renames files or commits Git.

## Additive migration and adoption

`0014_path_primary_articles` depends on the unchanged
`0013_reconcile_article_compatibility`. It removes code uniqueness, makes Article
ordering path-primary, and clarifies field help text. Path uniqueness remains.
No IDs, content, codes, paths, or existing blank-path records are rewritten.
The independent knowledge-app migrations need no change: paths live in canonical
JSON, and no filesystem-backed model is added to the common app.

**Historical prerequisite:** a database before 0013 must still satisfy that
already-existing migration's duplicate-code preflight. A host already at 0013
can directly reach 0014. If an earlier fs-vector DB contains duplicate codes,
the host must explicitly choose/record compatibility mapping (such as NULL for
genuinely source-only legacy codes) before traversing 0013. This code does not
fake migration records, rewrite applied files, or invent assignments. After
0014, duplicate codes no longer constrain the path model. A downgrade to 0013
with duplicates similarly requires host reconciliation; it is not promised safe.

Tests cover fresh migration traversal and upgrade starts at legacy 0009,
foundation status-0010, fs-vector source-0010, source-index-0011 and prior 0013.
An additional 0013-to-0014 test preserves an unmapped Article's ID/body/code and
then writes multiple distinct paths sharing its legacy code. Existing source
checkpoint/unit preservation tests remain. All earlier migration files have no diff.

`ArticleCompatibility.propose_import(path_mapping=...)` is a reviewed, explicit
mapping path, not a DB migration that changes source records. Invalid existing
paths require host corrections/mapping; no guessed normalization is applied.
Publication checks final path ownership and supports swaps inside one transaction;
source-managed Articles remain source-owned. Unmanaged embedding behavior stays.

## Validation

- Earlier full checkpoint before final edits: **827 passed, 20 skipped**.
- Focused path suite: **63 passed** (memory/DB/Git staging, both host policies,
  explicit committed Git round-trip, move/citation/index invalidation, path
  validation/collision/scope/denial, toolkit/commands and v2 receipt goldens).
- Final provider-free suite: **356 passed**, 62.53 seconds, using only the
  lightweight app on SQLite/memory plus synthetic Git reader fixtures.
- Final full isolated pgserver suite: **827 passed, 20 skipped**, 229.10 seconds.
  Final migration drift check: **no changes** in django_ergo or ergo_knowledge.
- Full configured Ruff passes for common modules and new path/Article/migration
  tests; selected formatting checks pass (25 files). Git whitespace checks pass.
  No repository-wide baseline lint claim.
- Minimal installed wheel passes the existing advanced memory/SQLite smoke and
  additional reviewed subtree moves, strategy/navigation, old citation resolution,
  rebuilt hybrid search, usage and v3 exports on **both** memory and SQLite.
  Package import resolves inside `/tmp/ergo-direction-minimal`, not the source tree.
  Installed distributions are only Django, django-environ, asgiref, sqlparse and
  django-ergo: no pgvector, psycopg, OpenAI or YAML installed.

The temporary standalone smoke script is `/tmp/ergo-path-wheel-smoke.py`; the
wheel is `/tmp/ergo-direction-wheel/django_ergo-0.1.0-py3-none-any.whl`.
Final wheel SHA-256: `31bbfafb63ec9260752f3c40b2b681101c7a93d34f8b41ebf864cdead46c17ad`.
It was rebuilt and its smoke rerun after the implementation commit.
The script derives the existing smoke from tests/test_knowledge_boundary.py,
then exercises moves and revision resolution against both virtual backends.
No shared production database or real provider was used.

Full validation command:

```bash
env -u DATABASE_URL -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u TEST_OPENAI \
  ERGO_TEST_ISOLATED=1 PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH=/tmp/ergo-direction-review-test-deps:src \
  /home/dev/p/boundcorp/cabal/.venv/bin/python -m pytest tests -q --tb=short \
  -o addopts='' --reuse-db --ds=tests.example_app.settings
```

The isolated settings create a temporary pgserver per process. The shared test
interpreter is read-only; test-only extra dependencies and wheel installs are in
/tmp. `--reuse-db` is confined to that disposable instance, not a shared host DB.

## Preserved sources and remaining limits

All 39 fs-vector source-file hashes match the prior reconciliation inventory;
its HEAD remains `5f3e5deed73edce9e28898749d3e0f2041488a34`. Both held Cabal
worktrees and all six planning checksums remain unchanged. The original direction
memo remains byte-identical. Main retains only its original untracked plan.
No auth/raw sessions/databases were copied, no original documents moved, and
no source checkout mutated. Old reports remain historical evidence.

Existing optional boundaries remain: legacy Article/repository DB models require
PostgreSQL/vector extras; optional persisted repository vectors remain 1536D.
The common virtual API stays provider-free unless semantic capabilities are
explicitly configured; no storage-dependent fallback is disguised as parity.
Memory stores/indexes/default usage are process-local, and Git publication is
host-owned. Host identity, tenancy, permission/UI/jobs/providers remain outside
Ergo. No Cabal orchestration implementation, push, deployment or reset occurred.

Current adoption docs: `docs/knowledge-paths.md`, `docs/knowledge-foundation.md`,
`docs/fs-vector-integration.md`. Two consuming-app policies remain in
`examples/knowledge_hosts.py`. Implementation is committed after final validation.
Earlier integration commits remain 10ec75b (foundation), fb7b861 (source-ref merge),
f666e10 (curated integration), and cc8b355 (historical handoff). No prior applied
migration was changed, and none of those historical reports was overwritten.
