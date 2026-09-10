# Ergo foundation implementation handoff

**Historical snapshot milestone, superseded by the subsequent implementation:**
see `ERGO_VIRTUAL_PARITY_IMPLEMENTATION_RECONCILIATION_2026-09-09.md` for the
implemented writers, ingestion/toolkit bridges, optional packaging and final
verification. Statements below describe the earlier milestone only.

Date: 2026-09-09 · Originating task: `01a08756-6fcd-7763-8ec0-09bd9f974dbf`

**Bounded foundation implemented and tested; uncommitted and not deployed.**
Review/implementation work is complete for this slice; no worker or background
task is continuing. Lightweight distribution packaging and broader universal
toolkit/writer integration remain incomplete, as detailed below.

Worktree: `/home/dev/orca/workspaces/django-ergo/codex-ergo-direction-review`
Base/unchanged HEAD: `0e9c9c2bae2cba5ea3de7f23cb8a97edb7181bbd`.

## Inventory informed the contract

Existing supported virtual knowledge is DB-authored Knowledgebase/Article data,
automatic embedding fields and semantic queries, hierarchy/navigation, admin,
write/suggest toolkits, conversation storage/rendering/import, approval-aware
turns, and conversation absorption into suggestions. Suggestions themselves
can accumulate in memory before applying to the DB. None requires filesystem
knowledge storage. Those APIs remain available; this change does not force
legacy Articles to acquire Git paths, manifests, source references or reviews.

The preserved direction memo's file-first recommendation is now subordinate to
the explicit no-filesystem requirement. Logical collection/document/revision
identity and content access are the common contract. Storage paths, Git refs
and YAML interpretation exist only in the Git adapter. The implementation
does not import the pending `fs-vector` tree or its migrations.

## Delivered

| Area | Review entrypoint |
| --- | --- |
| Immutable logical schema, provenance, references, review digest binding, validation and export/import | `src/django_ergo/knowledge/schema.py:69` |
| Backend protocol and genuinely process-local virtual corpus | `src/django_ergo/knowledge/backends.py:9` |
| Optional Django JSON snapshot persistence, explicit revision selection and integrity checks | `src/django_ergo/knowledge/database.py:8` |
| Host-selected committed Git/Markdown input, bounded reads, safe paths, ancestor body resolution | `src/django_ergo/knowledge/git.py:32` |
| Host authorization/audit/review-policy interface and shared cited retrieval | `src/django_ergo/knowledge/service.py:36` |
| Django management command for configured host service factories | `src/django_ergo/knowledge/management/commands/ergo_corpus.py:13` |
| Independent provider/vector-free Django app and JSON model migration | `src/django_ergo/knowledge/apps.py:4`, `src/django_ergo/knowledge/migrations/0001_initial.py:1` |
| Engineering-project and equipment-maintenance host-policy examples | `examples/knowledge_hosts.py:7` |
| Both-backend/host integration guide, runnable virtual example, exact parity matrix | `docs/knowledge-foundation.md:1` |

MemoryCorpus, DatabaseCorpus and GitCorpus all support the same validation,
lexical search, current-document reads, exact revision/digest citation
resolution, review checks, lifecycle exclusions, authorized history, export,
and Django command. All three run the same contract tests under both host
policies. Memory operations are tested with file/process access trapped;
DatabaseCorpus is also tested with in-memory SQLite, without a filesystem KB
or database file.

The schema is `ergo-corpus/v1`. Virtual/DB inputs are ordinary logical objects,
not YAML files. `ergo-source.yaml` is the Git adapter's authored serialization:
metadata stays there, with inline content or an explicit Markdown body path.
The export envelope contains the same normalized schema and its revision hash.
No competing wiki-frontmatter parser or implicit `kb.yaml` conversion is added.
Older experimental exports require explicit reviewed conversion; they are not
silently treated as compliant input. Historical citations resolve retained
content, including an ancestor Git body whose old path was deleted.

## Legacy fixes and upgrade behavior

- `search_garden_kb` now filters by the supplied user's ownership instead of
  selecting globally by garden-related names/descriptions.
- Article content/summary QuerySet searches retain their existing filters;
  vector and weighted multi-field queries exclude non-active Articles.
- New `Article.status` is additive and defaults to `active`, preserving
  pre-existing DB-authored rows and existing write/absorption behavior.
  Ordinary tool reads, TOCs, counts, prefetched results and the example API
  exclude archived/unpublished content. Explicit privileged ORM/admin access
  remains available; it is not silently turned into a filtered manager.
- Migration `src/django_ergo/migrations/0010_article_status.py:1` is tested by
  upgrading an existing virtual Article from `0009`, retaining its ID,
  hierarchy, content and KB association. Applied migrations are untouched;
  hierarchy uniqueness and implicit embedding behavior are preserved.
- Tests explicitly select the mocked OpenAI adapter for provider-error cases,
  rather than assuming every host's default embedding provider is OpenAI.

The optional `ergo_knowledge` app has its own migration lineage. A future
combination with the different unmerged `fs-vector` migration `0010` needs
deliberate migration reconciliation; this worktree must not be blindly merged
with that experiment.

## Validation evidence

1. **Full suite: 515 passed, 20 skipped, 141.51 seconds.** Includes legacy
   memory/handbook absorption, conversation/toolkit tests, new corpus contracts,
   access/lifecycle regressions and the upgrade migration test. Skipped tests
   retain the suite's real-provider/fixture gates; no live-provider success or
   production readiness is claimed.
2. **Final provider-free rerun: 93 passed, 8.05 seconds**, after the last scalar
   record-validation hardening. Includes both host policies across memory,
   database and Git; malformed records, stale reviews, denied scope/history/
   evidence access, invalid citations, database corruption, unsafe paths,
   symlinks, ancestor-path deletion, and identical logical export/import.
3. Startup test blocks imports of pgvector, psycopg, OpenAI, YAML, legacy Ergo
   models and conversation modules while running the virtual Django command.
4. `makemigrations ergo_knowledge --check --dry-run` reports no changes.
   New-code Ruff check and formatting checks pass; `git diff --check` passes.
   Modified legacy files retain **18 existing Ruff findings**, verified against
   their HEAD contents (lazy-import/style warnings); no repository-wide
   lint-clean claim is made.

Exact full-suite invocation used:

```bash
env -u DATABASE_URL -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u TEST_OPENAI \
  ERGO_TEST_ISOLATED=1 PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH=/tmp/ergo-direction-review-test-deps:src \
  /home/dev/p/boundcorp/cabal/.venv/bin/python -m pytest tests -q --tb=short \
  -o addopts='' --reuse-db --ds=tests.example_app.settings
```

Exact final provider-free invocation:

```bash
PYTHONDONTWRITEBYTECODE=1 /home/dev/p/boundcorp/cabal/.venv/bin/python -m pytest \
  tests/test_knowledge_corpus.py tests/test_knowledge_boundary.py \
  --ds=tests.knowledge_settings -o addopts='' -q --tb=short
```

The existing Cabal virtualenv supplied the interpreter/dependencies only; it
was not modified and no Cabal service was invoked. Its missing declared
`python-jose[cryptography]` API-example dependency was installed into the shown
temporary target with approval. Isolated test mode skips `.env`, ignores
shared database URLs, uses deterministic embeddings and a non-secret legacy
client placeholder, and creates disposable local PostgreSQL with cleanup.
`--reuse-db` avoids a transient drop-database warning from a lingering async
test connection; it does not reuse a shared service because each process gets
a new temporary server directory. Earlier collection/environment failures and
one incorrect new pagination assertion were addressed before the passing run.

## Exact limitations and remaining decisions

**Incomplete: minimal distribution installation.** Runtime separation is
implemented and tested, but ordinary installation of this distribution still
pulls its historical base psycopg/pgvector/OpenAI dependencies. Installing the
legacy `django_ergo` app still requires PostgreSQL/vector migrations. Only the
new `django_ergo.knowledge` app avoids those runtime/migration requirements.
The filesystem extra is optional; dependency removal/package splitting is not
completed, because changing the old installation contract deserves a separate
compatibility decision. No applied migration was rewritten to conceal this.

**Parity is precise, not universal object interchangeability.** Every newly
implemented common snapshot feature works without filesystem backing. Existing
DB/virtual Article features still work without files. However, a MemoryCorpus
or GitCorpus is not yet a drop-in replacement for Article in legacy semantic,
write, conversation or absorption toolkits. There is no automatic Article-to-
snapshot synchronization. Extending those toolkits should use logical content/
revision contracts across backends, not introduce mandatory files.

**Not implemented for any new backend:** common proposal/apply writers,
automatic ingestion connectors, durable operation/review transition logs,
cross-scope promotion, retention/erasure, semantic indexes for the new service,
HTTP/MCP transport, or Cabal/voice coordination. These are not hidden
filesystem-only capabilities. Hosts explicitly admit extracted text and own
authorization, trusted review receipts, redaction, provider/jobs/UI choices.
The persistence adapters themselves are trusted infrastructure, not public
authenticated write endpoints.

**Trust/scale:** one host scope per collection; snapshot validation scans a
bounded corpus each call. Search is title-weighted lexical substring scoring,
not BM25 or vector search. Historical content must be retained explicitly and
resolved against the recorded corpus revision. Hashes detect mismatches, not
independently authenticated truth. Logical revision labels must not be reused
for changed content; there is no universal writer enforcing that across future
snapshots yet. Policy/audit hooks are real enforcement points, but the host
must implement a trustworthy receipt ledger and durable audit destination.

Next review decisions: accept this additive API split; select the legacy
installation-compatible packaging strategy; then choose a bounded common
writer/Article-adapter slice if broader API interchangeability is required.

## Preservation and execution boundaries

No commits, branches, merges, deployments, publishing, production mutations,
original-document moves, worktree resets/deletions/archives, delegation,
orchestrators, extra agents, or usage-reset redemption occurred. Git commits
created by tests belong only to synthetic temporary fixture repositories.

Both Cabal worktrees remain preservation holds at their original locations:

- `/home/dev/p/boundcorp/django-ergo/.worktrees/4fe9433fa892`
- `/home/dev/p/boundcorp/django-ergo/.worktrees/743a48d58774`

All six original planning SHA-256 values still match the review inventory;
main still shows only its original untracked `docs/plan-fs.md`. The original
direction memo remains untouched, SHA-256
`1cb682c5a5ff19ca74f1ab53f063d83aa85b2d269e5c34a20b11b330fffdaec3`.
No auth material, raw sessions, `.cabal/codex-home` or existing application
databases were inspected/copied. Attachment metadata is still not a verified
backup, and the June/May documents' unverified-backup status is unchanged.
