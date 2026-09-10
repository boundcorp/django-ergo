# Ergo direction: reusable Django tooling, file-first knowledge

Date: 2026-09-09 · Originating task: `01a08756-6fcd-7763-8ec0-09bd9f974dbf`

**Disposition:** review complete; recommendations only, not implementation approval or merge clearance. One reviewer, local evidence inspection on devbox; no delegation or orchestration. Only this new memo is written. Original documents and both Cabal worktrees remain preservation holds.

## 1. Recommended direction

**Ergo should provide reusable knowledge services inside other Django applications:** validate, cite, retrieve, propose, review, and eventually apply changes to explicit knowledge collections. The host application supplies product identity and authority. Personal memory, project knowledge, and organizational handbooks are consumers—not definitions of Ergo itself.

Prefer a **file-first core with Django integration and optional projections/adapters**:

```text
host-authorized intake -> captured evidence -> proposed Markdown + review
                                            -> accepted file revision
                                            -> scoped retrieval
                                            -> optional rebuildable DB/search projection
                 versioned contract + linked operation/review records
```

Files hold accepted knowledge and its supporting artifacts. Evidence records what was captured; editable wiki pages record the currently accepted interpretation. Neither a raw capture nor a citation automatically establishes that a claim is true. Database projections must not become a second authoring authority for the same page.

| Credible direction | Benefit | Cost / assessment |
| --- | --- | --- |
| File-first Django plugin **(recommended)** | Human/agent-readable knowledge, reusable host integration, recoverable indexes | Requires explicit publication, scope, and compatibility contracts |
| File-only pilot | Fastest way to test authoring and review without ORM changes | Useful first experiment, but insufficient as the eventual Django integration product |
| DB-first plugin with Markdown import/export | Fits existing Article/admin tools and transactional writes | Files become an interchange format; round-trip conflicts undermine the stated flat-file preference |

Accepting the entire `fs-vector` tree is not a fourth architecture: it is an adoption shortcut with unresolved contracts and compatibility risks.

## 2. What direct inspection establishes

Paths in this section are source evidence, not proposed destinations.

1. **The reusable-app intent already has useful implementation seams.** Main exposes `EngineSpec`, `generate_once`, and `run_workflow_task`; `Toolkit` binds capabilities to supplied data. These are reusable building blocks, not a requirement that all KB consumers create conversations. The bundled public URL is a placeholder view; the fuller HTTP API lives in the example app. Do not equate README API/MCP ambitions with a finished file-memory API. Evidence: `/home/dev/p/boundcorp/django-ergo/src/django_ergo/conversation/runtime.py:1`, `/home/dev/p/boundcorp/django-ergo/src/django_ergo/conversation/toolkit.py:13`, `/home/dev/p/boundcorp/django-ergo/src/django_ergo/urls.py:1`, `/home/dev/p/boundcorp/django-ergo/tests/example_app/api.py:157`.

2. **Ownership/binding conventions are not universal authorization.** `Knowledgebase.owner_id` is an optional string; `KBToolkit` accepts a caller-supplied KB list. `search_user_kb` filters ownership, but `search_garden_kb` accepts a user while selecting globally by garden-related names/descriptions. Thus the previous blanket “user/binding scoped” description needs qualification. New public services must enforce host-supplied policy consistently; existing tool exposure needs a separate access audit before reuse. Evidence: `/home/dev/p/boundcorp/django-ergo/src/django_ergo/models.py:75`, `/home/dev/p/boundcorp/django-ergo/src/django_ergo/kb_toolkit.py:79`, `/home/dev/p/boundcorp/django-ergo/src/django_ergo/kb_tools.py:25`, `/home/dev/p/boundcorp/django-ergo/src/django_ergo/kb_tools.py:74`.

3. **The file contract genuinely diverges.** The plan proposes `ergo-source.yaml`; the builder emits and validates `kb.yaml`; sync scans `wiki/` and parses frontmatter without either manifest. Sync permits absent sources and arbitrary status strings. The proposal generator writes structured source-unit references, while the plan also permits relative source links. One shared contract matters more than selecting a filename in isolation. Evidence: `/home/dev/p/boundcorp/django-ergo/docs/plan-fs.md:95`, `/home/dev/orca/workspaces/django-ergo/fs-vector/src/django_ergo/filesystem_kb.py:975`, `/home/dev/orca/workspaces/django-ergo/fs-vector/src/django_ergo/sync.py:54`, `/home/dev/orca/workspaces/django-ergo/fs-vector/src/django_ergo/sync.py:107`.

4. **Projection is promising but does not implement reviewed publication.** Sync uses committed snapshots, UUID identity, transactional reconciliation, a checkpoint race guard, and missing-not-deleted records. However, it records frontmatter `status` separately from retrieval state and sets encountered documents active; `visible_to_retrieval()` checks tracking state, not reviewed/archived page status. An archived declaration alone therefore does not exclude a present page through that filter. No evidence/review gate precedes projection. Instance save/delete guards are not protection against all ORM writes. Evidence: `/home/dev/orca/workspaces/django-ergo/fs-vector/src/django_ergo/sync.py:128`, `/home/dev/orca/workspaces/django-ergo/fs-vector/src/django_ergo/models.py:131`, `/home/dev/orca/workspaces/django-ergo/fs-vector/src/django_ergo/models.py:338`.

5. **Indexing has outrun the knowledge lifecycle.** Repository search combines exact, PostgreSQL lexical, vector, and relation candidates; even lexical use of that entrypoint requires an embedding provider with the recorded fingerprint and 1536 dimensions. Wiki proposal generation requires an indexed source and a conversation run. Its review packets are useful, but they are not an enforced apply/review log. Existing conversation absorption returns in-memory suggestions whose apply method writes DB Articles, not canonical files. Evidence: `/home/dev/orca/workspaces/django-ergo/fs-vector/src/django_ergo/repository_search.py:22`, `/home/dev/orca/workspaces/django-ergo/fs-vector/src/django_ergo/repository_wiki.py:55`, `/home/dev/p/boundcorp/django-ergo/src/django_ergo/kb_pipelines.py:42`, `/home/dev/p/boundcorp/django-ergo/src/django_ergo/kb_suggest_toolkit.py:157`.

6. **The historical-citation defect is confirmed by source tracing, not a fresh reproduction.** Capture reads the cited commit/path, but validation tests path membership in the current repository manifest. Renamed/deleted ancestor paths are consequently rejected. Fix against commit-specific captured evidence when salvaging the exporter; do not let this localized defect determine the product architecture. Hash validation establishes internal consistency, not independent authenticity. Evidence: `/home/dev/orca/workspaces/django-ergo/fs-vector/src/django_ergo/filesystem_kb.py:751`, `/home/dev/orca/workspaces/django-ergo/fs-vector/src/django_ergo/filesystem_kb.py:1094`, `/home/dev/orca/workspaces/django-ergo/fs-vector/src/django_ergo/filesystem_kb.py:1195`.

## 3. Minimal core versus host ownership

Keep these as module/service boundaries first, not a new framework or immediate package split.

| Concern | Ergo responsibility | Host responsibility / optional adapter |
| --- | --- | --- |
| Identity and access | Stable collection/document/evidence IDs; require an authorized context; reject scope mismatches | Users, service principals, organizations, projects, membership, permission decisions, repository credentials |
| Canonical knowledge | Versioned file schema, reference resolution, lifecycle validation, deterministic committed reads | Provision storage and trusted publication refs; backups, retention choices, filesystem access |
| Intake | Bounded evidence envelope, provenance and validation results; proposal contract | Upload UI, source credentials, approved extraction/redaction, domain-specific connectors |
| Review and changes | Artifact/version binding, transition checks, conflict detection, operation record format | Reviewer eligibility, approval policy, review UI, authorized writer, scheduling |
| Retrieval | Scoped page/evidence reads and lexical baseline; structured citations and revision freshness | HTTP authentication, product presentation, quotas and serving policy; optional BM25/vector backend |
| Execution | Explicit callable operations and results; idempotent jobs where applicable | Celery/RQ/cron/task choice, retries, provider selection, credentials, observability destination |

Do not introduce Ergo-owned Tenant/Project models, a mandatory frontend, voice server, scheduler, or provider account. A host may associate its Project with an Ergo collection without Ergo importing that host's models. A repository is a storage locator, not inherently a tenant.

**Packaging caveat:** the current package is not dependency-light: `pyproject.toml` requires PostgreSQL/pgvector/OpenAI, Article has vector fields, and migration `0000` creates the vector extension. “Optional search” is a recommended runtime boundary, not a claim that today's installed app supports a vector-free database. Isolate new file services from ORM/provider imports; defer dependency/app separation to a compatibility-reviewed change rather than rewriting historical migrations. Evidence: `/home/dev/p/boundcorp/django-ergo/pyproject.toml:20`, `/home/dev/p/boundcorp/django-ergo/src/django_ergo/models.py:286`, `/home/dev/p/boundcorp/django-ergo/src/django_ergo/migrations/0000_create_vector_extension.py:1`.

## 4. Proposed coherent file and governance contract

**Recommended default, awaiting user acceptance:** use `ergo-source.yaml` for authored collection identity/layout/schema version; reserve `kb.yaml` for generated export inventory. Both must consume the same page/evidence schema. This is one contract with distinct source and export records, not two competing manifests. Source frontmatter must not be inferred differently by sync, search, and export.

Minimal conceptual layout:

```text
knowledge/ergo-source.yaml       collection identity, schema version, scope key
knowledge/raw/                  admitted evidence + capture metadata
knowledge/wiki/                 maintained Markdown; index.md is navigation
knowledge/schema/               versioned declarative rules/profile
knowledge/reviews/              proposal versions, decisions, verification artifacts
knowledge/operations/           append-only event files, one immutable entry per event
```

- **Evidence:** stable ID, scope, source locator/revision, capture actor/time, media type and byte hash; optional excerpt selector. Append new captures rather than overwrite. URL-only sources are weaker than retained captures and should be labeled unavailable/unverified when they cannot be resolved.
- **Pages:** stable ID, title, type, lifecycle status, explicit scope, source references, and supersession links when relevant. Start with `note` and `decision`, not the entire old taxonomy. Keep claim support visible through Markdown footnotes/anchors; do not require a claim table, graph, or numeric confidence model. Human review assesses whether sources actually support claims; syntactic lint cannot prove that.
- **References:** resolve from collection root by stable evidence identity plus optional selector, not ambiguous page-relative `../` conventions. Git references retain commit and original path; paths are locators, not identity. Cite the file revision independently of ephemeral DB/source-unit IDs. Scope document identity by collection so separate host installations can consume the same corpus.
- **Policy:** bind each collection to one host-registered scope initially; pages/evidence repeat that scope and mismatches fail closed. Metadata declares classification, never grants access. Host-selected schema/rules may constrain behavior but untrusted corpus instructions must not execute or expand permissions. Git ancestry proves reachability, not reviewer approval.
- **Review:** `intake -> proposal -> review -> apply -> verify` requires linked artifacts, not a workflow engine. An approval targets the exact proposal/base revision and policy version; a subsequent semantic edit invalidates that approval. Publication verifies hashes and reviewer authority through the host. Start with manual file/PR review and a single authorized writer, not automated merge/write-back.
- **Operations:** record operation ID, actor, timestamp, reason, source references, affected page IDs, proposal/review references, outcome, and base/result revision as applicable. A later verification event can reference the published commit without a self-referential commit hash. File history alone does not explain the review decision. Denied accesses go to a host-controlled audit destination without leaking restricted content into shared logs. Ordinary Git files are auditable, not tamper-proof.

Separate publication status from source availability: only reviewed active pages enter default retrieval; draft/stale/superseded/archived pages require explicit authorized historical access. A missing source should become visibly unavailable, not silently delete history. Cross-scope promotion remains disabled initially; later it is a separately approved derivative with explicit provenance and redaction, not a label flip granting access to private evidence.

**Memory controls:** eventual consumers need inspect/“why remembered,” explicit remember proposals, correction/supersession, and “stop using this” withdrawal. Withdrawal must invalidate derived retrieval/context caches; it is not physical erasure from Git, backups, or previously delivered context. Append-only evidence is the normal editing rule, not a promise of indefinite sensitive-data retention. Decide exceptional redaction/erasure policy before broad conversation capture. No such operation is performed by this review.

## 5. Retrieval, ingestion, and agent integration

Start with deterministic path/title/text search over authorized active pages; direct grep is adequate for the pilot. Add BM25 when ranking needs it, embeddings after measured semantic misses, graph traversal only after demonstrated relational misses. Record a small host-supplied query set with expected pages/citations before changing retrieval. Raw evidence drill-down is a separate authorized operation, not indiscriminate raw-session search.

Results should expose collection/page ID, scope, title, bounded excerpt, lifecycle, canonical revision/path, and supporting evidence references. A derived index additionally reports its indexed revision/schema/parser/provider fingerprint and stale status. Rebuilding from accepted files must recover equivalent identities, visibility, references, and lexical results. Host identities, ACLs and job credentials are operational configuration—not reconstructable from public corpus files and never exported as knowledge.

Use Python services first; Django management commands and later HTTP/MCP/toolkit adapters call those same services. Conceptual operations are `validate`, `search`, `get_page`, `get_evidence`, then `propose_change` and authorized `apply_change`—not promises of existing APIs. Require host authorization before search candidate selection, counts, snippets, evidence access, exports, and writes. A local agent with unrestricted filesystem access can bypass application policy; separate checkouts/OS permissions are necessary for genuinely distinct private scopes.

An ingestion extension accepts host-admitted content plus provenance/scope and returns an evidence/proposal artifact with warnings and an idempotency key. The host selects extractors, MIME/size limits, secret scanning, retention and providers. Begin with explicit Markdown/text capture only; PDFs, email, issue trackers, and selected conversation excerpts are later adapters. Never turn arbitrary imported instructions into tool authority, and never automatically “remember everything.”

### Two different consuming host apps

1. **Engineering project portal:** the host's Project membership authorizes a collection backed by an approved repository alias/ref. An engineer explicitly captures a sanitized build finding; a coding agent proposes a decision page citing it. Host PR review approves the exact diff, Ergo validates it, and the portal exposes cited search to the next agent session. Project routing, CI jobs, agent execution, and reviewer identity stay with the portal.
2. **Equipment-maintenance Django app:** the host owns companies, sites, technicians and document access. Initially an administrator admits a text excerpt of a maintenance bulletin into a site-scoped Git-backed collection; later the host's PDF extractor can supply the same evidence envelope. A reviewed troubleshooting note becomes searchable in the technician UI with bulletin/page provenance. No software repository semantics, chat session, embedding service, or Cabal model is required by the knowledge contract; a different company's technicians cannot read its evidence.

Future Cabal voice conversations can use the same authorized knowledge services while Cabal owns conversation UX, project dispatch and delegated execution. Voice/Codex/Orca inspiration establishes a possible consumer, not today's implementation scope. Preserve the June thin chatbot-facade research separately; storage validation must not depend on that facade.

## 6. Small phased foundation and salvage

**Phase 0 — settle one contract, not every old plan.** Approve the defaults in section 4 and a non-sensitive project pilot. Design fixtures containing one evidence item, one decision, one superseding revision, and linked review/operation artifacts. No production migration, private-memory taxonomy, or review daemon is needed to settle this.

**Phase 1 — smallest useful implementation slice:** a shared committed-corpus validator and scoped read/search service, exposed through one Django management-command entrypoint and exercised by two minimal host-policy fixtures. Read a configured approved revision, validate schema/identity/scope/source and review references, and return one active page with its evidence citation. Author and approve fixture artifacts manually; implement no source writer, LLM pipeline, new database model, embedding backend, or automatic ingestion yet.

Acceptance: repeated reads are deterministic and network-free; an allowed host receives the cited page; another scope receives no page/evidence/count leak; unapproved or stale-review revisions, broken references, duplicate IDs and unsafe paths are rejected; superseded pages leave default results; moving the checkout preserves identity; no corpus or DB writes occur. This is a small usable Django integration, not just another design document. Automated claim-truth verification is not implied.

**Phase 2 — one-way projection only when a host needs it.** Extract source/document tracking and reconcile accepted revisions into a Django read model. Test create/edit/rename/missing/reactivation, scope and lifecycle filtering on every entrypoint, idempotence, dry-run, failed-batch rollback and full rebuild. Keep projection checkpoints distinct from indexing checkpoints; use explicit post-transaction host jobs. Add file proposal/apply automation only as a subsequent bounded change with stale-base rejection, serialized publication and auditable retry/recovery.

**Phase 3 — measured adapters.** Portable exports, additional ingestion connectors, BM25/vector indexing, richer review UX and conversation helpers follow demonstrated needs. No Cabal integration, watcher, graph, broad session import, or orchestration is implied.

| Existing material | Salvage recommendation / gate |
| --- | --- |
| Six Cabal planning docs and `docs/plan-fs.md` | Preserve all originals; reuse the evidence/synthesis/rules distinction, scope denials and artifact gates. The fixed taxonomy, confidence fields, local embedding model and artifact-session priorities remain proposals, not commitments. |
| `paths.py`, `git_snapshot.py`, `sync.py`, `KnowledgeSource`/`SourceDocument` | Extract patterns only after aligning contract, lifecycle and authorization. Repository alias/path/ancestry checks are useful but not a complete trust policy. |
| Existing Article behavior and migration `0010` | Do not transplant wholesale: it changes hierarchy uniqueness/order, adds unbackfilled paths and disables implicit embeddings globally. Prefer additive tracking/read-model changes; preserve DB-authored Articles and legacy APIs until explicit compatibility migration tests pass. |
| Snapshot builder/validator | Keep as an optional export utility, not the mandatory ingestion core. Remove any requirement to scan sessions from the foundation path; reconcile schema, fix ancestor-path evidence and document integrity limits before acceptance. |
| Source-unit indexing, relations, migration `0011`, LLM wiki packets | Preserve experiments; reuse citation/result/review-artifact ideas without inheriting AST/vector/LLM dependencies. Defer adoption until measured retrieval need. |
| Conversation runtime, provider seams, June facade idea | Preserve independently. Reuse existing runner/renderer boundaries when a host needs chat; do not invent parallel transcript models or couple chat to file storage. |

Later DB-to-file conversion must be explicit and per collection: export for review, assign stable identity, validate counts/content/references, then choose a single authority and disable competing edits. Do not auto-convert existing rows or make all Articles file-managed. A compatibility adapter can retain legacy hierarchy addressing without declaring paths permanent identity. Relevant migration evidence: `/home/dev/orca/workspaces/django-ergo/fs-vector/src/django_ergo/migrations/0010_knowledgesource_sourcedocument_alter_article_options_and_more.py:54`.

## 7. User decisions, not inferred approvals

1. Accept the file-first reusable Django direction and proposed source/export contract, or prefer a strictly file-only pilot?
2. Accept one host-defined **project scope per collection** initially, with repositories as storage and cross-scope promotion disabled? This avoids hard-coding repo/user/ticket as universal Ergo tenancy.
3. Accept human approval of all semantic publication initially, minimal `note`/`decision` pages with visible references, and operation/review artifacts in the first slice? Defer calibrated confidence and richer claim records.
4. Choose the first non-sensitive host corpus. Private memory, retention/erasure guarantees, provider choices and automated writers require later explicit decisions before those capabilities are enabled.

Preservation itself is already instructed, not a decision to reopen. No worktree cleanup is proposed.

## 8. Evidence status, preservation, and verification limits

Inspected main `/home/dev/p/boundcorp/django-ergo` at `0e9c9c2` (untracked `docs/plan-fs.md`), feature `/home/dev/orca/workspaces/django-ergo/fs-vector` at `5f3e5de` (substantial tracked edits and untracked implementation/reports), and this initially clean worktree at `0e9c9c2`. No applicable `AGENTS.md` was found in inspected ancestor paths or repository trees, excluding runtime/dependency directories. Read all three requested feature reports and all six original planning docs, then traced their key claims into source and test definitions.

Both preserved Cabal worktrees remain at `3faf919`, with no tracked changes shown and these untracked planning deliverables:

| Worktree / document | SHA-256, independently recomputed in this review |
| --- | --- |
| `4fe9433fa892` / `2026-06-13-structured-call-chatbot-helper-research.md` | `cb3a0d66a817ac3631b41c9237979113c2670f91627d96eeb96654cda3cd77be` |
| `743a48d58774` / `adoption-plan-gaps-checklist.md` | `b892d65203da4257e1116c40169ffd2a39cb982f3f98712b52d585bbad7038c7` |
| `743a48d58774` / `gstack-new-features-reevaluation-2026-05-26.md` | `af502c183dd6ed42414d9cb3be21d8ea8628d20a15a2244ebb7e24fb761066fb` |
| `743a48d58774` / `investigation-findings-adoption-gaps.md` | `4e4a7794ab26504b39ac6e6e1c5f25663c96368ad2ace420fe57c58ca2aa4bcf` |
| `743a48d58774` / `prioritized-adopt-learn-avoid-roadmap.md` | `06a7ae7cecdecf1751e69f6badccad14f8704dbb63a1d106fe363dc8a71d2d93` |
| `743a48d58774` / `remaining-gaps-next-experiments.md` | `693dd90e648f9beb8f5b8fa9bfeb0f76c4a8a7888dbb4f450145d093b4214692` |

The paths are `/home/dev/p/boundcorp/django-ergo/.worktrees/4fe9433fa892/docs/plans/` and `/home/dev/p/boundcorp/django-ergo/.worktrees/743a48d58774/docs/plans/`. All six checksums match the preservation report. The June memo and May 26 re-evaluation still lack verified backups. Attachment identifiers for the other documents are metadata, **not independently verified backup content**; this corrects the preservation report's stronger “second known copy” wording. No attachment was fetched or authenticated. Untracked planning remains valuable even when tracked code adds nothing unique.

Planning-source anchors: `adoption-plan-gaps-checklist.md:112` (retrieval), `adoption-plan-gaps-checklist.md:209` (claims), `adoption-plan-gaps-checklist.md:294` (artifact chain), `adoption-plan-gaps-checklist.md:381` (scope/retention), `remaining-gaps-next-experiments.md:34` (small fixtures), `gstack-new-features-reevaluation-2026-05-26.md:96` (artifact sessions), and the June memo at line 42 (facade boundaries), under those original directories. External gstack release claims were not independently re-researched; only their locally recorded design reasoning informs this recommendation.

Prior reports record 18 focused tests passing and a full run of 422 passed, 20 skipped, 5 failed, attributed to missing async-test support; they also report focused lint success but 68 broader new-file Ruff violations. **These are prior reported results, not fresh verification.** This review inspected test coverage and environment loading; `tests/test_sync.py:41` covers basic projection/missing visibility, not the proposed complete governance/host contract. Test settings load `.env` and may initialize PostgreSQL. No tests, application startup, database access, build, or network/provider call was run, preserving the review-only boundary. No blanket ready-to-merge conclusion follows.

No auth material, raw sessions, `.cabal/codex-home` contents, or databases were read or copied. No implementation, commit, merge, deletion, reset, archive, deployment, document move, or usage-reset redemption occurred. Review is complete; no background or continuing work remains.
