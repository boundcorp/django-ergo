# Optional repository adapters and common knowledge API

The common `CorpusService` remains the public backend-neutral API. Memory,
database and committed-Git adapters support validation, intake/review/apply,
citations, lifecycle, hierarchy/strategy, lexical/semantic/weighted/hybrid
retrieval and usage. Git publication is explicit: stage reviewed changes in
memory or DB, export the canonical corpus, then let a host-authorized publisher
write/commit it and select the ref. Applying a staging proposal never commits Git.
See `knowledge-foundation.md` for executable host policies and all common methods.

## Installation and authority

- `django-ergo`: provider-free memory services and `django_ergo.knowledge` on
  SQLite or another supported Django database; no filesystem/Git/YAML required.
- `django-ergo[filesystem]`: common Git reader and standalone portable snapshot
  Python utilities. These only import YAML when the optional adapter is used.
- `django-ergo[legacy,filesystem]`: existing `django_ergo` app, Article APIs,
  repository projection/index tables and its management commands. PostgreSQL
  with vector remains required by historical legacy migrations; the independent
  knowledge app does not inherit those migrations.

Declare extras explicitly before refreshing an existing host's lock file.
Legacy automatic Article embeddings are retained for unmanaged rows. Managed
wiki projections intentionally skip automatic generation; use `index_article`
or `index_articles` with an explicit provider. Common semantic search uses its
own host-selected index/provider independently of source storage and dimensions.

Repository services are trusted infrastructure, like ORM access. The host must
authorize the chosen KnowledgeSource and destination before invoking them or
binding RepositoryToolkit. Repository aliases come from
`DJANGO_ERGO['KNOWLEDGE_REPOSITORIES']`; never let untrusted callers select arbitrary
paths or refs. Management commands run with operator authority. Source content,
frontmatter, commit ancestry and old 'reviewed' labels do not confer host grants.

## Three representations, one authored common contract

1. `ergo-corpus/v1`, `/v2` and `/v3` are logical common records. `ergo-source.yaml` is
   just the Git adapter's serialization of those same records; virtual corpora
   use the same JSON/schema without that file. JSON exports retain the identical
   semantic revisions. Summary/legacy codes/strategy use v2; paths use v3.
   Existing v1/v2 hashes remain until an explicit semantic revision.
2. Legacy `wiki/*.md` frontmatter is an **input adapter format**, not an alternate
   common canonical schema. `project_commit` preserves its old committed-source
   projection interface and stable UUID/path/checkpoint history. Paths are optional
   primary logical Article paths. Hierarchy codes are legacy compatibility only;
   missing paths remain explicitly unmapped, never inferred from codes.
3. Legacy `kb.yaml` describes a **portable repository evidence bundle**, not an
   authored common manifest. Builder output is review material, not automatically
   approved KB state. Snapshot validation is internal consistency, not authenticity.

Explicitly import an old portable bundle into any common writable backend:

```python
from django_ergo.filesystem_kb import read_wiki_records
from django_ergo.knowledge.ingestion import propose_wiki_import

proposal = propose_wiki_import(
    service, read_wiki_records(host_authorized_bundle),
    provenance=host_capture_provenance, reason="Review prior wiki material",
)
```

For a genuinely no-filesystem host, pass an iterable of
`{'metadata': {'id': 'logical-id', 'title': 'Guide', 'status': 'current'},
'content': 'Body'}` records from the host API instead. No YAML parser, file or Git
is accessed by this common importer. A declared foreign scope is rejected.
The captured original record, including old citation metadata, becomes retained
evidence; each page cites that exact capture. It does **not** pretend that old
unit IDs/claims are independently resolved or approved. Follow normal exact-digest
review and proposal-receipt/apply; then export the one canonical schema. For
independent underlying evidence, additionally admit host-resolved captures and
review explicit sources through the normal common proposal API.

`ArticleCompatibility` similarly imports old Article content through review.
Missing/archived source projections cannot become active through import. Publishing
back to managed Articles is forbidden: publish their source instead. Unmanaged
Article publication accepts code-free paths and retains automatic
embeddings and original caller types. There is no hidden bidirectional writer.

## Repository workflows retained

```text
build_repository_kb REPOSITORY NEW_OUTPUT --commit TRUSTED_REF
validate_filesystem_kb BUNDLE
sync_knowledge SOURCE_ID --dry-run
sync_knowledge SOURCE_ID
index_repository SOURCE_ID --lexical-only
index_repository SOURCE_ID
propose_repository_wiki SOURCE_ID --user REVIEWER --output NEW_PACKET
```

Use host settings and manage.py for these commands. No commands above were run
against original repositories in this integration; tests use synthetic fixtures.
Build/validate can also be called as standalone Python functions with only the
filesystem extra. Raw session capture is opt-in in the legacy builder; do not
feed auth files, raw sessions or private data without a separate explicit host
admission/redaction policy. Integration did not ingest any original session data.

- Sync consumes committed Markdown, never dirty working-tree content. It keeps
  identity across renames, preserves prior paths, hides missing and non-active
  statuses, rejects unknown statuses, and clears stale vectors after changes.
  Instance/admin managed-write guards do not replace host authorization or defend
  against privileged raw SQL/QuerySet bulk writes.
- Repository indexing preserves AST/Markdown source units, reconciliation and
  directed relationships. Lexical-only indexing/search needs no embedding provider
  or dimension setting. `search_repository(..., mode='lexical')` is explicit;
  default hybrid preserves the original interface and checks fingerprints.
  RepositoryToolkit uses lexical search for a lexical-only index.
- The optional persisted repository vector index retains its historical 1536D
  PostgreSQL schema. This restriction does not apply to common MemoryVectorIndex,
  DB/memory/Git corpus retrieval, or provider-free repository lexical mode.
- Wiki synthesis retains inspected-evidence requirements, goal coverage and
  review packets. It requires the host-selected conversation engine/model. It is
  repository-specific evidence acquisition, not a second generic memory API or
  mandatory filesystem step for virtual ingestion/review/toolkit workflows.
- Historical source-unit citations bind captured bytes to the cited commit/path,
  even if absent at HEAD. Captured SHA-256/length and Git blob IDs are verified.
  Unit hashes are still unit-level assertions, not reconstructed whole-file hashes;
  bundles are not signed or tamper-proof. Symlink bundle entries are rejected.

## Additive upgrades

Never rename, fake or edit an already-applied migration to choose a branch.
Both `0010_article_status` and the original long-named fs-vector 0010 remain;
the original fs-vector 0011 also remains byte-identical. They change distinct
fields and descend from the same 0009. `0012_merge_corpus_and_sources` joins
both leaves; `0013_reconcile_article_compatibility` restores logical hierarchy
ordering/indexes/uniqueness while keeping optional path uniqueness and nullable
codes for source-only Articles. Existing IDs, content and paths are not rewritten.

Supported/tested upgrade starts: legacy 0009, foundation status-0010,
fs-vector source-0010, and source-index-0011; fresh installs traverse both branches.
The separate knowledge-app 0001–0003 migration history is unchanged.

Fs-vector temporarily permitted duplicate non-null hierarchy codes. If such
rows exist, 0013 stops with an actionable error before changing them. A host must
decide which codes to retain/reassign, or explicitly choose NULL for source-only
rows, then retry. The migration does not guess, delete, or rename data. Duplicate
codes are a genuine adoption decision, not silently repaired. Downgrading a source
database with nullable codes to pre-fs schema is not promised.

The later additive `0014_path_primary_articles` removes code uniqueness again
and makes path ordering primary. It retains the path uniqueness constraint and
all existing data. Hosts already at 0013 can retain duplicate codes after 0014;
hosts starting earlier still traverse the unmodified 0013 precondition. See
`knowledge-paths.md` for explicit mapping and compatibility details.

## Operational distinctions

Common in-memory corpus/index/default usage sinks are process-local. Durable
virtual corpus/usage uses the independent DB app; file usage journals are another
optional sink. Snapshot exports do not include host identities, ACLs or credentials.
Independent usage and publication do not form a distributed transaction; inspect
`UsageRecordingError.committed` and the operation log before retrying.

Corpus and repository indexes are different derived views with different identity
and performance contracts. Rebuild each explicitly after its source changes;
repository DB services query their recorded snapshot, not uncommitted files.
Nothing is pushed or deployed by this integration.
