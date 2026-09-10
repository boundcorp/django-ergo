# Ergo virtual-KB parity reconciliation and implementation handoff

**Historical milestone.** The user subsequently required advanced features in
the new common API, not just their preservation in Article APIs. See
`ERGO_COMMON_API_ADVANCED_PARITY_IMPLEMENTATION_2026-09-09.md` for the completed
common hierarchy/strategy, semantic/hybrid retrieval, usage and compatibility
extension; the limitations and completion statement below describe this earlier
milestone only.

Date: 2026-09-09 · Task: `01a08756-6fcd-7763-8ec0-09bd9f974dbf`
Worktree: `/home/dev/orca/workspaces/django-ergo/codex-ergo-direction-review`

**Implemented, uncommitted, not deployed.** This supersedes the snapshot-only
milestone's limitations, not the preserved original direction review. The
user's requirement is authoritative: file-first cannot mean file-required.
No original planning document or held worktree was moved, overwritten or
discarded.

## Reconciliation: what “all features” actually covers

There are two supported no-filesystem APIs, not one interchangeable model:

1. **Existing DB-authored KBs:** Knowledgebase/Article plus the existing
   conversation and toolkit APIs. These retain their vector/provider/database
   requirements, but never require filesystem KB backing.
2. **New logical corpora:** MemoryCorpus and DatabaseCorpus.workspace now
   support actual intake, proposals, review/apply, correction/withdrawal,
   durable revision provenance, cited retrieval and agent tools. The first
   milestone's missing write/ingestion bridges have been implemented, not
   reclassified as future work.

GitCorpus is a read adapter to that same logical contract. Shared authoring can
stage a Git snapshot in either writable backend; a host publisher must export
and commit it to make Git authoritative again. A staged apply is explicitly
NOT a Git commit. No Git/path/manifest requirement enters virtual workflows.

### Directly inspected workflow inventory and executable evidence

Paths are worktree-relative. All test files below run in the full suite unless
identified as an isolated-install smoke check.

| Existing or newly required workflow | Supported no-filesystem API | Evidence / distinction |
| --- | --- | --- |
| Create KB, create/update/delete articles, automatic summary/embedding fields | Existing Knowledgebase/Article ORM; kb_write_toolkit.create_article/update_article/delete_article | tests/test_kb_write_toolkit.py, tests/test_openai_fields.py; preserves hierarchy allocation and existing hard-delete semantics |
| Direct write tools, approval-aware generic tool execution | KBWriteToolkit; tool_registry; WorkflowEngine / conversation runner | tests/test_kb_write_toolkit.py, tests/test_tool_registry.py, tests/test_workflow_engine.py, tests/test_conversation_runner.py |
| Accumulate suggestions, inspect/selectively apply/clear | Existing KBSuggestToolkit.get_suggestions/apply_suggestions/clear_suggestions | tests/test_kb_suggest_toolkit.py, tests/test_example_company_handbook.py; DB virtual workflow preserved |
| Conversation-to-KB ingestion and personal memory | Existing absorb_conversation(session, Knowledgebase, engine) | tests/test_kb_pipelines.py, tests/test_example_personal_memory.py, tests/test_example_company_handbook.py |
| KB organization strategy and tree gap analysis | Existing KBStrategyToolkit get/update/propose-tree/status | tests/test_knowledge_legacy.py::test_virtual_strategy_write_and_gap_analysis_hide_archived; no filesystem dependency |
| Hierarchy/TOC, list, direct reads, scoped garden/user queries | Existing KBToolkit and kb_tools | tests/test_kb_toolkit.py, tests/test_kb_tools.py, tests/test_knowledge_legacy.py; ownership and archived exclusions corrected |
| Semantic content/summary search, weighted multi-field/vector/hybrid search | Existing Article QuerySet and embedding providers | tests/test_knowledge_legacy.py, tests/test_embedding_providers_simple.py, tests/test_openai_fields.py; caller filters retained, no virtual feature removed |
| Conversation import, storage and session management | ImportService.import_auto(decoded_data, user), ClaudeCLIImporter.import_conversation(data, user), SessionManager | tests/test_conversation_import.py, tests/test_conversation_models.py, tests/test_conversation_manager.py; data API does not need source files |
| Import command reading JSON/JSONL/directories | Existing import_conversations command | tests/test_import_command.py; source-file reading is an intake adapter, not filesystem KB backing; decoded-data API above is the no-FS equivalent |
| Rendering, historical session tools, summarize/compact | ConversationRenderer, HistoryToolkit, conversation pipelines | tests/test_conversation_renderer.py, tests/test_conversation_toolkit.py, tests/test_conversation_pipelines.py |
| Engine/runtime tools, approvals, context/usage bookkeeping | generate_once, run_workflow_task, existing engine adapters and ConversationKBUsage | tests/test_conversation_runtime.py, tests/test_conversation_generate.py, tests/test_kb_usage_tracking.py; no corpus paths added |
| Example RAG chunking, context optimization, feedback learning | Existing examples.rag_examples and thumbs_feedback_system classes with virtual Articles/data | tests/test_rag_system.py, tests/test_thumbs_feedback.py; examples preserved, not promoted to a new core promise |
| New shared validation, exact citations, review checks, export/import and authorized retrieval | CorpusService over MemoryCorpus, DatabaseCorpus or GitCorpus | tests/test_knowledge_corpus.py, both host policies across three readers |
| New evidence ingestion from host-extracted uploads/notes/transcripts | CorpusService.intake; prepare_absorption | tests/test_knowledge_writes.py::test_absorb_review_apply_correct_archive_and_cite; evidence and page are one pending artifact |
| New immutable proposals, authenticated review/rejection, conflict-safe apply | Proposal.to_dict/from_dict; CorpusService.propose/review/apply | tests/test_knowledge_writes.py: rejection/tamper, host-denial, scope/history rewrite, base/reason receipt binding, conflict tests |
| New explicit remember/correct/withdraw/re-publish controls | CorpusService.revise; draft/active/archived/superseded/stale revisions | tests/test_knowledge_writes.py: draft publication and correction/archive history; no silent erasure |
| New no-FS cited agent toolkit and conversation ingestion bridge | CorpusToolkit via extra_tools; absorb_corpus_conversation(session, prepared_toolkit, engine) | tests/test_knowledge_pipeline.py, six host/backend cases, both engine schema adapters; sends host-redacted capture, not raw session |
| New Django management write/read operations | ergo_corpus alias intake/review/apply/operations plus validation/search/get/resolve/export/TOC | tests/test_knowledge_writes.py::test_virtual_management_intake_review_apply |
| New durable virtual authoring, revision reopening, transactional log | DatabaseCorpus.workspace with CorpusHead, CorpusRevision, CorpusOperation | tests/test_knowledge_writes.py::test_database_cas_rolls_back_failed_log_and_reopens; SQLite-memory and PostgreSQL |
| Minimal installation without optional packages | django_ergo.knowledge only; plain django-ergo wheel | tests/test_knowledge_boundary.py plus fresh /tmp wheel installation and SQLite migration/write/search/citation/log smoke check |

The existing FK-based ConversationKBUsage remains an Article/Knowledgebase
feature. CorpusToolkit returns no fake legacy FK bindings; logical collection,
scope, actor and revision appear in its policy audit events and operation log.
Hosts correlate those with their session metadata. Likewise Article admin,
hierarchy strategies and vector ranking remain available on **virtual DB KBs**,
not magically implemented on the lightweight MemoryCorpus object.

The experimental fs-vector repository crawler/index, wiki generation and
portable-builder work remain preserved, unmerged experiments. They are not
silently adopted as supported core features or removed. Their repository/Git
operations are storage/source adapters; any later adoption must keep logical
identities and no-FS equivalents. No blanket fs-vector ready-to-merge claim.

## Contract and implementation decisions

- Hosts own principals, scope membership, source admission, policy, receipt
  issuance, UI, jobs and providers. Core rejects a backend identity mismatch
  and checks permission before content loading.
- Corpus schema is `ergo-corpus/v1`. Authored `ergo-source.yaml` hydrates to
  exactly that schema; generated JSON export wraps the same normalized object
  and hash. Virtual inputs are objects, not fabricated YAML/manifests.
- Proposals contain the exact base and append-only candidate. Every applied
  change needs a host-accepted document review AND receipt bound to the entire
  reviewed proposal digest. Rebasing or changing justification invalidates the
  receipt. Tools can propose but cannot issue their own approvals.
- Memory writers use a lock/CAS; DB writers transactionally update the head,
  append the snapshot and operation event. A failed log write rolls everything
  back. Historical document revisions remain citation-resolvable after edits
  or withdrawal. Reusing a revision or changing document kind/scope fails.
- One independent JSON model migration was already added for snapshots; a
  second additive migration supplies durable heads and operation records.
  No historical legacy migration is rewritten. Legacy 0009-to-0010 upgrade
  coverage preserves Article identity/hierarchy/content and adds active status.
- Search remains deterministic title-weighted lexical substring ranking.
  Existing DB vector features are preserved, not replaced by this baseline.
  No mandatory embeddings, graph or model provider enters the new core.
- Two host fixtures implement independent project membership vs company/site
  membership. Their mutable approval sets are demonstration host ledgers, not
  a public approval API. Real hosts must authenticate reviewers and persist
  receipts and audit events.

Implementation entrypoints:
`src/django_ergo/knowledge/{schema,changes,backends,database,git,service,toolkit,ingestion}.py`,
`src/django_ergo/kb_pipelines.py`,
`src/django_ergo/knowledge/management/commands/ergo_corpus.py`.
Integration guide: `docs/knowledge-foundation.md`.
Host examples: `examples/knowledge_hosts.py`.

## Packaging and installed-app compatibility

**Implemented, not deferred:** base requirements are now Django/django-environ.
`django-ergo[legacy]` retains psycopg, pgvector and OpenAI for the existing app;
`[openai]` and `[filesystem]` select SDK and YAML adapters independently.
ToolConfig is model-independent and re-exported from its old tools import path.
The lightweight app does not load legacy models/migrations/providers.

**Required host upgrade action:** declare `django-ergo[legacy]` in existing
hosts' requirements/lockfiles before a clean environment sync. Existing
packages are not uninstalled by this code, but lockfile pruning can remove
them unless the extra is selected. Legacy Article databases still require
PostgreSQL/vector and their existing engine configuration. No automatic
SQLite conversion or provider substitution is claimed.

A wheel was built locally, not published. A fresh /tmp environment installed
only Django, django-environ, asgiref, sqlparse and django-ergo; the installed
wheel ran SQLite-memory migrations, evidence intake, toolkit proposal,
review/apply, search, citation resolution and operation history. No OpenAI,
pgvector, psycopg or YAML distribution was installed there. This verifies a
real optional installation boundary, not merely blocked imports in a fat venv.

## Verification

- Full isolated suite: **575 passed, 20 skipped**, 150.58 seconds.
- Latest provider-free shared suite: **146 passed**, 14.18 seconds.
- Focused existing-session/corpus bridge: **6 passed**, 4.89 seconds.
- New-code Ruff check passes; no repository-wide baseline lint-clean claim.
- Lightweight migrations: `makemigrations ergo_knowledge --check --dry-run`
  reports no changes. Previously applied legacy migration files have no diff.
- Initial full rerun had six new test setup errors (missing django_db marker),
  not application failures. Marker corrected; focused and full reruns pass.
- Providers in the full suite use deterministic/mocked behavior. Twenty skipped
  cases and a passing suite are not evidence of live provider compatibility.
  Interpreter used: Python 3.12.11; older advertised Python versions were not
  recertified by this run.

Full-suite command (disposable PostgreSQL, no shared DB or real provider key):

```bash
env -u DATABASE_URL -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u TEST_OPENAI \
  ERGO_TEST_ISOLATED=1 PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH=/tmp/ergo-direction-review-test-deps:src \
  /home/dev/p/boundcorp/cabal/.venv/bin/python -m pytest tests -q --tb=short \
  -o addopts='' --reuse-db --ds=tests.example_app.settings
```

Provider-free command:

```bash
PYTHONDONTWRITEBYTECODE=1 /home/dev/p/boundcorp/cabal/.venv/bin/python -m pytest \
  tests/test_knowledge_corpus.py tests/test_knowledge_writes.py \
  tests/test_knowledge_boundary.py --ds=tests.knowledge_settings \
  -o addopts='' -q --tb=short
```

## Exact remaining distinctions and decisions

No identified supported baseline virtual workflow was removed or made
file-dependent. The identified missing common write/intake/review/toolkit APIs
are implemented. This is **not universal object interchangeability** between
Article, MemoryCorpus and GitCorpus.

- Git commit/write-back and operation-history publication are host adapter
  operations. Staged changes do not modify the original Git repository.
- Pending/rejected proposals and review audit events need host persistence.
  Applied DB operation logs are durable; memory logs are process-local.
  Snapshot exports preserve document reviews, not the separate operation log.
- Corpus reads scan bounded snapshots (10,000 revisions; 1 MiB/document;
  16 MiB/corpus). Incremental indexes/BM25/new vector projections are not
  implemented in the logical service; legacy virtual DB semantic APIs remain.
- There is no automatic Article/snapshot synchronization, universal admin UI,
  HTTP/MCP server, cross-scope promotion, erasure/retention engine, binary
  extractor, external source crawler, or Cabal voice orchestration. These
  were not removed supported common features. Source authorization/redaction
  and scope-promotion/erasure policy remain genuine host/product decisions.
- Withdrawal excludes future normal retrieval; it does not scrub old evidence,
  previously emitted contexts, or backups. All first-slice governed writes,
  including withdrawals, require approval. A host wanting immediate emergency
  withdrawal or physical erasure needs a separately specified policy/API.

No commits, deployments, merges, production writes or original-document
moves/deletions/resets occurred. Tests created only synthetic temporary Git
commits and disposable databases. Both original Cabal worktrees remain under
preservation hold; six planning documents retain their original checksums.
The June and May26 documents still lack verified backups; attachment metadata
was not treated as independently verified backup. No auth/raw session material
was copied.

Final verification includes a rebuilt wheel at
`/tmp/ergo-direction-wheel/django_ergo-0.1.0-py3-none-any.whl`, reinstalled only
into `/tmp/ergo-direction-minimal`; its imported module path was verified to
be the installed wheel, not this source checkout. The full virtual memory/DB
smoke check passed again. Ruff and migration drift checks pass; git diff has
no whitespace errors. The six held-document SHA-256 values were rechecked
against the original inventory and all match. The original direction review
retains SHA-256 `1cb682c5a5ff19ca74f1ab53f063d83aa85b2d269e5c34a20b11b330fffdaec3`.

**Execution status: this bounded implementation and verification are complete;
no work, tests or worker processes are continuing. No commit or deployment.**
