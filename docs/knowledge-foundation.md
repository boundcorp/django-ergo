# Backend-neutral knowledge foundation

## Advanced features through the same public KB API

Storage does not select a different feature surface. MemoryCorpus,
DatabaseCorpus and committed GitCorpus readers all expose these operations
through CorpusService:

| Operation | Common API |
| --- | --- |
| Read layout and individual placements | navigation(prefix), table_of_contents(prefix=...), by_path_prefix(prefix), get_by_path(path) |
| Create with explicit placement | create_page(..., path=... OR parent_path=..., name=...) |
| Correct summary or move placement | revise(document_id, summary=..., reason=...), move(document_id, path, reason=...), move_tree(prefix, destination, reason=...) |
| Read/revise strategy and propose trees | get_strategy(), propose_strategy(...), propose_tree(...), get_tree_status() |
| Semantic field queries | semantic_search_content(query), semantic_search_summary(query) |
| Weighted semantic queries | multi_field_semantic_search(query, weights=...) |
| Precomputed-vector queries | vector_search_content(vector), vector_search_summary(vector), multi_field_vector_search(vector, weights=...) |
| True lexical/semantic combination | hybrid_search(query, weights=..., lexical_weight=0.5) |
| Public search dispatch | search(query, mode="lexical" OR "semantic" OR "hybrid", weights=...) |
| Context attribution and history | record_usage(context_id, mode=...), with_usage_context(context_id), usage(context_id=...) |

Creation/strategy/move helpers return proposals, not implicit publications.
The existing review/apply gate and stale-base conflict checks cover all these
changes. Logical paths are unique across heads, including archived reservations.
Prefix navigation respects slash-segment boundaries. Paths never require files
or Git. Empty paths are explicitly unmapped compatibility records; codes never
become filenames automatically. Codes remain optional, potentially ambiguous
legacy metadata. Explicit old code allocation/read APIs remain compatibility-only.
Strategy is one reviewed Markdown document; named trees use path headings.
Tree counts include only active mapped pages. `move_tree` revises those headings
in its review batch; arbitrary prose/links are not rewritten. Old code plans stay
readable as content; use `get_tree_status(legacy=True)` or `propose_legacy_tree`
explicitly. See [path contract and adoption](knowledge-paths.md).

### Optional retrieval configuration

```python
from django_ergo.knowledge.retrieval import MemoryVectorIndex
from django_ergo.knowledge.service import CorpusService

service = CorpusService(
    backend,
    host_policy,
    principal,
    embedding_provider=host_embeddings,
    provider_id="host-model:version:configuration-fingerprint",
    vector_index=MemoryVectorIndex(),
)
service.rebuild_index()
results = service.multi_field_semantic_search(
    "pump reset", weights={"content": 0.7, "summary": 0.3}
)
```

An embedding provider implements `generate_embedding(text)` and
`get_dimensions()`; the existing pluggable Ergo providers satisfy this shape.
No provider SDK or global provider selection is imported by the shared
retrieval implementation. The host must grant `index` before embedding pages
and `semantic` before embedding/searching queries, in addition to normal
search authorization. This makes external data transmission/cost an explicit
host capability, not an implication of choosing DB or filesystem storage.

The supplied MemoryVectorIndex is a disposable exact-cosine projection.
Rebuilding embeds nonempty content, summary and title of **active page heads
only**, after authorization/review validation. The projection is bound to
collection, scope, corpus revision, dimensions and host provider fingerprint.
Stale revisions or changed fingerprints fail clearly; call rebuild_index
again. Archived content cannot remain retrievable through an old index.
Rebuild is explicit, not a hidden billed action on each mutation/search.

Weighted cosine scores use nonnegative finite weights, default content 0.6 /
summary 0.4. Missing/empty fields are omitted and remaining weights normalized
per document; a document with no selected fields is excluded. Weights accept
content, summary and title. Vector inputs must be finite, nonzero, matching
dimension lists/tuples. Text queries require a provider; precomputed vector
queries need only a matching populated index/fingerprint.

Hybrid combines normalized lexical title/body counts and cosine similarity,
controlled by lexical_weight in [0, 1]. Its field weights apply to the semantic
component. Unlike the old Article hybrid alias, this is actual lexical/vector
fusion. Default lexical search is unchanged and provider-free; explicit
lexical weights can also select summary/title/content. Missing provider/index,
unbuilt/stale index or changed fingerprint raises CapabilityUnavailableError;
there is no silent fallback masquerading as semantic search.

Indexes are replaceable host-injected objects with rebuild(snapshot, documents,
provider, provider_id), check(snapshot, provider_id), and scores(snapshot,
documents, query_vector, weights, provider_id). Results are re-bound to
authorized current document references, never accepted as arbitrary content
from an index. The bundled projection is process-local; it is not a persistent
ANN database or a PostgreSQL index migration. Host workers must rebuild or
provide a compatible persistent index. Caller-supplied summaries are canonical;
embedding vectors remain derived and do not alter corpus hashes.

### Usage persistence is independent of corpus persistence

Every successful cited read/search, TOC/strategy read, proposal and publication
records structured usage through an injected UsageStore. Records contain a
host context ID, actor ID (empty if a legacy read policy lacks actor()),
collection/scope/revision, mode, timestamp and references—not query text or
source bodies. The `usage` permission gates history inspection.

- MemoryUsageStore is the **default for every backend** and is process-local.
  Share a sink across request-bound services when shared local history is wanted.
- DatabaseUsageStore persists through the lightweight app's additive
  0003_corpus_usage migration. It can be used with memory, DB or Git KBs.
- FileUsageStore is an optional POSIX, locked/fsynced JSONL journal, also usable
  with any KB backend. Its host-selected parent directory must exist; new files
  use mode 0600. Malformed journals fail; the bounded 16 MiB journal requires
  explicit host rotation. It is independent of the committed Git manifest.
- A host may implement record(event) and read(collection_id, scope, context_id)
  using its own telemetry store. All supplied sinks scope reads and reject
  conflicting reuse of an event identity.

CorpusToolkit's runner hook binds a cloned service to the actual conversation
session ID. Other toolkits/services do not inherit that context accidentally.
Existing ConversationKBUsage FK rows are still recorded for Article toolkits.
Usage is observation, not authorization, reviewer authenticity or content
retention policy.

A failing usage sink does not silently disable tracking. UsageRecordingError
reports `committed=True` if the canonical publication already succeeded.
Inspect operations() before retrying such a write. The applied DB operation
record remains transactional with its snapshot; the independent usage sink
is intentionally not a distributed transaction with canonical storage.

### Explicit Article compatibility

ArticleCompatibility(service, knowledgebase, authorize) bridges old callers
without changing their ORM/QuerySet return types or rewriting migrations.

`authorize(principal, knowledgebase, action)` must independently authorize
`export` (all legacy rows, including archived content) or `publish`.
A logical corpus scope grant is not permission to copy another Article KB.

- propose_import(provenance=..., reason=..., path_mapping=...) creates retained evidence and
  proposed pages with original Article IDs, summary, status and hierarchy, plus
  the KB strategy. Existing paths are retained; an optional ID-to-path mapping
  is explicit host input, not guessed from codes. It does not invent approvals. The host reviews/applies
  that artifact through the common service.
- publish() transactionally upserts accepted heads back into the bound Article
  KB, retaining original UUIDs; new logical IDs map deterministically to UUIDs.
  Existing Article field/provider behavior still runs. Missing rows are kept,
  never deleted, and foreign IDs or conflicting path identities fail. Repeated
  legacy codes do not constrain paths; code-free path pages also publish.
- Publication checks the source fingerprint captured by import under row locks.
  Concurrent legacy edits cause an explicit conflict, not silent overwrite.
  A newly constructed bridge must receive expected_source_revision from an
  explicitly reviewed source_revision() capture.
- This is explicit import/export, not automatic two-way synchronization.
  Select an authoring authority; reimport/review deliberate legacy corrections.

### Commands and file publication

The existing alias-based command also accepts get_strategy, get_tree_status,
paths --prefix, get_path --path, navigation --prefix, legacy hierarchy --prefix,
usage --context, create_page/move/move_tree/propose_tree/propose_strategy (JSON stdin), and
rebuild_index. Search accepts --mode, --weights JSON, and explicit
--rebuild-index to build/query in the same command process. A fresh
MemoryVectorIndex in each alias factory does not survive separate CLI calls.

GitCorpus reads committed authored v1/v2/v3 records. To author, use a governed
MemoryCorpus/DatabaseCorpus workspace seeded from that Git snapshot. Apply
changes there, export the canonical corpus object, then let the host publisher
write and commit the authored representation and select a trusted ref. Direct
GitCorpus apply is rejected clearly. No service call claims a staging apply
committed files. The tests exercise this publication round trip in a synthetic
repository, including strategy and semantic reads of the published v2 corpus.
Usage journals and derived indexes have separately selected persistence; a
Git commit is neither required for virtual APIs nor a substitute for usage
persistence.


See also the workflow-by-workflow reconciliation in
`ERGO_COMMON_API_ADVANCED_PARITY_IMPLEMENTATION_2026-09-09.md`.

This is a reusable Django integration, not a standalone memory application.
**A filesystem is never required by the common corpus contract.** The earlier
direction memo is preserved as review history; its file-first recommendation
is superseded by the user's explicit virtual-KB parity requirement.

## Inventory and compatibility

Before this change, the supported runtime stored Knowledgebases and Articles
in Django/PostgreSQL. It provided automatic embedding fields, vector and
weighted multi-field search, hierarchy navigation, admin, write/suggest
toolkits, conversation storage/rendering/import, approval-aware turn execution,
and conversation-to-KB suggestions. Suggestions accumulate in memory and apply
to DB Articles. Neither that memory workflow nor those DB KBs requires Git,
YAML, Markdown files, or a filesystem corpus. The main branch did not include
the pending `fs-vector` implementation.

Those capabilities remain available. This foundation adds a separate logical
snapshot API rather than silently converting existing Articles or replacing
their authoring/search behavior. `Article.status` defaults existing/new rows
to `active`; other states are excluded from normal search and read tools.
Article UUIDs, embeddings, and legacy write/suggest APIs remain available.
Hierarchy uniqueness is removed by additive migration 0014; old code lookups
require unambiguous codes. Direct ORM/admin access remains privileged management
access and can intentionally inspect archived records. Archiving does not
erase prior transcripts or already delivered context.

The legacy `search_garden_kb(user, ...)` now selects only that user's KBs, like
`search_user_kb`. It no longer searches other owners by garden-related names.
Content/summary QuerySet searches now retain the caller's filters. Low-level
model-wide field helpers still require the host to provide an authorized
QuerySet when scope matters; they are not authentication endpoints.

| Feature | MemoryCorpus | DatabaseCorpus | GitCorpus |
| --- | --- | --- | --- |
| Common schema and structural validation | Yes | Yes | Yes |
| Scope-authorized lexical search and active-page reads | Yes | Yes | Yes |
| Exact revision/digest citations and evidence resolution | Yes | Yes | Yes |
| Review-digest binding plus host review acceptance | Yes | Yes | Yes |
| Lifecycle exclusions, explicit authorized history | Yes | Yes | Yes |
| Portable logical export/import and Django commands | Yes | Yes | Yes |
| Hierarchy, summaries, reviewed strategy and tree gap analysis | Yes | Yes | Yes; mutations staged explicitly |
| Semantic content/summary, weighted vector and hybrid retrieval | Same optional provider/index | Same optional provider/index | Same optional provider/index |
| Scope/context usage tracking | Memory, DB or file sink | Memory, DB or file sink | Memory, DB or file sink |
| Persistence | Host-owned; process-local by default | JSON snapshots in host DB | Committed authored records/bodies |
| Revision selection | Current immutable snapshot | Pinned digest or live CAS workspace | Host-configured Git ref, resolved once per load |
| Intake, proposals, review, apply, correction, withdrawal | Yes, process-local | Yes, transactional snapshots/head/operation log | Same APIs in a staged MemoryCorpus or DB workspace; host publishes exported authored data |
| Curator toolkit and conversation absorption bridge | Yes | Yes | Yes, against the staged workspace |
| Legacy Article vector/write/conversation tools | Existing DB workflow remains separate from all three snapshot adapters |

This is common read/write/ingestion parity, **not a claim that a MemoryCorpus
or GitCorpus can be substituted directly for the legacy Article model in
every old toolkit**. The common API now exposes semantic/vector/hybrid search
through storage-independent optional indexes, plus hierarchy/strategy and usage.
There is no replacement transcript store; existing conversation storage is reused.
The compatible CorpusToolkit and absorption bridge are implemented below.
The existing no-filesystem Article workflows continue unchanged apart from
the access/lifecycle corrections. A host can use them alongside snapshots and
explicitly admit selected content into a reviewed snapshot; there is no
implicit DB-to-file conversion or automatic DB-Article/snapshot synchronization.

## Integration and installation boundary

For only the new services/command/optional JSON persistence, use:

```python
INSTALLED_APPS = [
    # Host apps...
    "django_ergo.knowledge",
]
```

The `ergo_knowledge` app has independent migrations with no dependency
on auth, legacy Ergo, vector extensions, or provider SDKs. Memory/Git services
and commands need no database queries or migration. DatabaseCorpus requires
this app's migration and a Django database with JSONField support; tests cover
in-memory SQLite and the full application's PostgreSQL environment. Importing
the schema, MemoryCorpus and CorpusService does not initialize Django.

Existing installed applications keep `"django_ergo"` and may additionally
install `"django_ergo.knowledge"`. Apply the additive `0010_article_status`
migration for the legacy app. No previously applied migration is rewritten.
The unrelated unmerged `fs-vector` migration numbered `0010` is NOT included;
any future combination requires an explicit migration reconciliation review.

**Installation:** `pip install django-ergo` now installs Django/django-environ
only. For the existing Article/conversation app use
`pip install 'django-ergo[legacy]'` (psycopg, pgvector, OpenAI). Keep that extra
in existing hosts' requirements/lockfiles before their next clean environment
sync. An ordinary upgrade does not uninstall already installed SDKs, but a
dependency-sync tool may prune them unless the extra is declared. No legacy
model, vector field or applied migration is removed or rewritten. New and
existing legacy databases still require PostgreSQL and its vector extension;
they are NOT automatically converted to SQLite. Missing vector imports on
legacy app startup produce an actionable installation error.

`django-ergo[openai]` selects the optional SDK independently. Other providers
remain host-selected. These extras change installation selection, not engine
behavior or credentials. The legacy WorkflowEngine still constructs its client
on import and requires its existing configuration; use the lightweight app
when that legacy engine is unnecessary.

`django-ergo[filesystem]` adds PyYAML for GitCorpus. Virtual and DB operations
do not import YAML, Git, OpenAI or pgvector through the new API. Git itself is
an external executable required only for GitCorpus.

## The logical contract

`django_ergo.knowledge.schema` defines immutable values:

- `Snapshot(collection_id, scope, documents, heads)` identifies a collection
  and contains retained document revisions plus one current reference per
  document. IDs are host-provided strings, not paths or database primary keys.
- `Document` carries logical document ID/revision, scope, kind (`page` or
  `evidence`), title, text, status, provenance, source references, and optional
  review. Lifecycle is `draft`, `active`, `stale`, `archived`, or `superseded`.
- `Provenance(origin, source_revision, actor, captured_at)` records an admitted
  capture. `origin` may be a host object ID/URI, not necessarily a file. Capture
  time requires an ISO timestamp with timezone. Captures can represent text
  excerpts of other formats; binary extraction remains the host's job.
- `Reference(document_id, revision, digest)` resolves exact retained content
  within the collection. It is independent of Git commit/blob/path identity.
  Cross-collection/scope references are deliberately unsupported initially.
- `Review(reviewer, decision, reason, policy_version, reviewed_digest)` binds
  a review to the document's semantic digest. Active pages require approved
  reviews and evidence references. Evidence capture does not become factual
  truth merely because it has a hash or reviewer.

The document digest includes its identity, content, lifecycle, scope,
provenance and sources, but excludes its review to avoid a recursive hash.
Changing any covered field requires a new matching review. The corpus revision
is a SHA-256 digest of its canonical JSON representation, including reviews;
document/head ordering does not change it. **Hashes detect inconsistencies,
not forgery by an attacker who can replace content and hashes together.**

The host must retain required historical document revisions in the snapshot.
Missing history is a validation/resolution failure, never silently replaced by
current content. Citations should be stored with the collection ID AND returned
`corpus_revision`; construct the service against that snapshot when resolving
later. Logical revision labels cannot be reused through the shared writer:
every operation appends revisions and retains all prior content. Digest
mismatches fail closed. DatabaseCorpus.store returns a pinned reader;
DatabaseCorpus.workspace initializes/reopens a live compare-and-swap head
without resetting an existing one. Direct privileged ORM edits remain outside
the writer's trust boundary, with payload integrity checked on load.

The shared JSON-compatible object format is `ergo-corpus/v1` for the original
records, or `ergo-corpus/v2` when summaries, hierarchy codes or strategy records
are present, or `ergo-corpus/v3` for logical paths (also duplicate legacy codes).
Empty new fields are omitted so existing v1/v2 hashes and proposal receipts do
not change. Assigning/moving a path creates a newly reviewed revision. Older
readers are not expected to read v3; the new reader accepts all three and rejects
path fields smuggled under an earlier declaration.

```python
payload = snapshot.to_dict()
validated = Snapshot.from_dict(payload)
```

No files are involved. The generated export envelope is
`ergo-corpus-export/v1`, containing `revision` and the same `corpus` object.
`export_snapshot`/`import_snapshot` are low-level trusted functions;
`CorpusService.export()` also enforces host export and history permissions.

## A genuinely virtual example

```python
from dataclasses import replace
from django_ergo.knowledge.backends import MemoryCorpus
from django_ergo.knowledge.schema import Document, Provenance, Review, Snapshot
from django_ergo.knowledge.service import CorpusService

capture = Provenance("host:bulletin:42", "edition-1", "uploader",
                     "2026-09-09T12:00:00Z")
evidence = Document("bulletin", "v1", "acme:west", "evidence",
                    "Pump bulletin", "Reset with the blue button.",
                    "active", capture)
page = Document("reset", "v1", "acme:west", "page", "Pump reset",
                "Use the blue reset button.", "active", capture,
                (evidence.reference,))
page = replace(page, review=Review("maintenance-review", "approved",
               "Checked the bulletin", "site/v1", page.content_digest))
snapshot = Snapshot("handbook", "acme:west", (evidence, page),
                    (evidence.reference, page.reference))

# Supply authenticated identity and verified review receipts from the host.
service = CorpusService(MemoryCorpus(snapshot), host_policy, authenticated_user)
results = service.search("reset")
```

For durable no-filesystem storage of that same admitted snapshot:

```python
from django_ergo.knowledge.database import DatabaseCorpus

# Trusted host intake, after its own write authorization/redaction checks.
backend = DatabaseCorpus.store(snapshot)
service = CorpusService(backend, host_policy, authenticated_user)
```

`store` is not an authenticated public upload endpoint. As with direct ORM
access, the host must guard this low-level persistence call. Data admission is
explicit: use `Snapshot.from_dict` for normalized extractor outputs and record
source origin/revision and reviewer receipts. There is no automatic raw-session
capture, secret scanning, or model invocation.

## Git/Markdown adapter and canonical authored schema

The host configures the repository, publication ref, collection ID and scope;
callers cannot choose an arbitrary repository through the command. The adapter
reads only committed blobs, rejects unsafe paths/symlinks, bounds input size,
and allows historical body commits only if ancestors of the selected commit.
It ignores uncommitted edits. Git reachability does not itself grant review or
scope authority.

`ergo-source.yaml` is the authored serialization of the **same logical object**.
Each document has either inline `content` or adapter-only `content_path`, never
both. `content_commit` may accompany `content_path` for a retained historical
body whose old path no longer exists at the current commit. The adapter resolves
the body and removes the path/commit fields before common schema validation.
Paths are repository-relative, not relative to the manifest's directory.
Keep timestamp values quoted strings in manually authored YAML.
`provenance.source_revision` remains explicit capture metadata, not an implicit
claim inferred from a path.

Keep identity/lifecycle/review/source metadata in the manifest and Markdown
bodies in separate files; no independently interpreted wiki frontmatter schema
is introduced. Example conversion from the logical object (host authoring):

```python
payload = snapshot.to_dict()
record = payload["documents"][0]
body = record.pop("content")
record["content_path"] = "raw/bulletin-v1.md"
# A host authoring tool writes body and YAML, obtains review, then publishes.
```

The foundation does not consume the incompatible experimental `kb.yaml`
inventory or auto-detect old formats. Its generated logical export uses the
same normalized corpus schema for all backends; an old-artifact conversion must
be explicit and reviewed. This resolves divergence by choosing one semantic
contract, not by imposing YAML on virtual sources or pretending prior formats
already conform. The full old portable builder and its dependencies were not
transplanted. A regression covers ancestor evidence whose path was deleted.

## Host authorization and two consumer examples

`HostPolicy` requires `allows(principal, collection_id, scope, action)`,
`accepts_review(principal, collection_id, scope, review)`, and
`audit(principal, event)`. Only literal `True` grants access. The host owns
authenticated identities, memberships, policy versions, and a trusted ledger
of accepted review digests; a self-authored reviewer string is not sufficient.
Audit failures propagate rather than silently bypassing the hook. Events contain
collection/scope/action/outcome, not queries, snippets or raw captures.

`examples/knowledge_hosts.py` demonstrates two different host policies:

1. An engineering portal maps project membership to a collection, and permits
   historical/export access only to reviewers. Its approved-digest set stands
   for host-verified PR review receipts.
2. A maintenance app checks BOTH company and site membership. Supervisors may
   inspect history; approval receipts belong to its maintenance review process.

Tests exercise **both hosts against all three backends**, including a technician
from another company with the same site name. Example audit lists are fixtures,
not a production durable audit implementation.

Permission checks precede corpus loading for denied collection actions. The
initial trust granularity is one scope per collection, not per-page ACLs.
Trusted adapters internally load whole bounded snapshots; the service never
returns evidence text without evidence permission. Historical revisions
additionally require history permission; default reads/search exclude all
non-active heads. Default search indexes curated pages, not raw evidence.
Do not hand an untrusted caller backend objects, direct ORM access, or a
filesystem checkout and expect the service to restrict those independent paths.

## Django command and retrieval behavior

Register host-owned zero-argument service factories, not untrusted paths or
client-supplied user/scope values:

```python
ERGO_CORPORA = {"project-kb": "my_host.knowledge.command_service"}
```

The factory returns a CorpusService with the host's authorized command identity,
policy and selected backend. OS/management-command access is trusted; HTTP
adapters must construct services from their own authenticated request context.

```text
python manage.py ergo_corpus project-kb validate
python manage.py ergo_corpus project-kb search --query "pump reset"
python manage.py ergo_corpus project-kb get --document reset
python manage.py ergo_corpus project-kb resolve --document bulletin --revision v1 --digest <digest>
python manage.py ergo_corpus project-kb export
```

Results include collection/scope/corpus revision, exact document citation,
source references and provenance. Search is a deterministic bounded lexical
baseline: case-folded query terms, title-weighted substring counts, stable ID
tie-breaking, at most 100 results and 400-character excerpts. It is **not BM25,
semantic search, or a replacement for existing Article vector APIs**. Full
validation scans a bounded snapshot on each operation; large-corpus indexes and
caches are intentionally absent. Provider selection/jobs remain host concerns.

## Verification and remaining boundaries

Run provider-free tests using an environment with Django/pytest/PyYAML:

```text
python -m pytest tests/test_knowledge_corpus.py tests/test_knowledge_boundary.py --ds=tests.knowledge_settings -o addopts='' -q
```

The startup boundary test blocks imports of YAML, provider/vector packages and
legacy models while running the virtual Django command. Separate tests trap
file/process access during MemoryCorpus operations. In-memory SQLite exercises
durable adapter behavior without creating filesystem KBs or database files.

Legacy/full-suite tests require the existing declared dev dependencies and
PostgreSQL/pgvector. `ERGO_TEST_ISOLATED=1` skips `.env`, ignores DATABASE_URL,
and starts disposable PostgreSQL under a fresh temporary directory, cleaning
the server/data on exit. It selects deterministic embeddings and a clearly
non-secret placeholder for the legacy client's import-time key requirement.
Unset real provider keys and keep `TEST_OPENAI` unset when using this mode.
This is opt-in and does not change normal host settings.
The migration test upgrades an existing virtual Article from `0009` to `0010`.

Not implemented: automatic external ingestion connectors, content erasure/retention engines,
cross-scope promotion, automatic bidirectional Article snapshot synchronization,
HTTP/MCP transports, or Cabal/voice
coordination. These remain common capabilities to design for every backend,
not features to reserve for filesystem KBs. A persisted approval in a corpus
is checked, but this reader does not itself authenticate who originally wrote
that record; the host policy must verify its trusted receipt.


## Writing, reviewing and ingesting without files

Use `MemoryCorpus(snapshot)` for process-local authoring, or
`DatabaseCorpus.workspace(snapshot)` for a durable live workspace.
The latter initializes a head only if absent; reopening with an old seed does
NOT reset an existing head. `DatabaseCorpus.store(snapshot)` remains a pinned
read-only reader. Both shared writers retain historical documents and reject
reused logical revisions, scope changes and document-kind changes.

The public service API is:

- `intake(content=..., title=..., provenance=..., reason=...)`: propose retained
  evidence from host-authorized extracted text. This never dereferences a
  source URI or reads an uploaded path.
- `propose(documents, reason=...)`: prepare one atomic change-set of new
  document revisions. `Proposal.to_dict()/from_dict()` transport the base,
  candidate, actor and reason as a versioned JSON artifact.
- `revise(document_id, reason=..., content=..., status=...)`: correct a
  document or change lifecycle with a new revision, not destructive deletion.
  Set draft to active for publication, or active to archived/superseded/stale
  to stop normal retrieval. Restoring a page also requires fresh approval.
- `review(proposal, reviews)`: validate a host-issued approval/rejection
  against exact digests and emit a host audit review event.
- `apply(reviewed_proposal)`: require host apply permission and the exact
  approved proposal receipt, then atomically compare-and-swap the base revision
  and persist the new snapshot plus operation record.
- `operations()`: authorized operation history, including actor, reason,
  sources, affected documents, reviews, base/result revisions and timestamp.

All applied changes, including evidence intake, drafts and withdrawals, need
approval in this first governed API. This does not retroactively change legacy
Article CRUD. Rejecting a proposal never publishes it. Proposals stay
host-owned artifacts until apply; persist pending/rejected artifacts and review
events in the host's review UI/job/audit store. Memory operation logs are
process-local; DB operation logs participate in the publication transaction.
They are auditable records, not tamper-proof against a privileged DB operator.
Exporting a snapshot retains document review records but not the operation
log: export `operations()` separately if moving authoring history.

### Host policy for writes

In addition to read actions, implement `allows` for `intake`, `propose`,
`review`, `apply`; implement `actor(principal)` with the host's stable actor ID,
and `accepts_proposal(principal, proposal)` with a trusted receipt ledger.
The receipt must bind `proposal.revision`, which includes the exact base,
candidate, author, justification and reviews. Editing a reason or rebasing
requires a new receipt, even if a document digest happens to be unchanged.
`accepts_review` still independently checks document review digests.

Proposal artifacts contain retained history and evidence, so preparing,
reviewing or applying one additionally requires `history` and `evidence`.
Do not grant these merely to make a public chatbot work. The example policies
allow these only to project reviewers/site supervisors. Hosts can choose a
different explicit role split. A public reader can still search and read
without receiving unpublished history.

`examples/knowledge_hosts.py` demonstrates two independent host identity
models (project membership and company/site membership), both with a trusted
digest set and a separate reviewed-proposal receipt set. The sets are fixtures,
not a production receipt issuer. Only the host's authenticated review code may
populate them; never accept receipt lists or policy code from tools or corpus
metadata. The service does not self-certify agent suggestions. Host audit
failures abort authorization/review rather than proceeding silently.

### Agent and ingestion integration

```python
from django_ergo.knowledge.ingestion import prepare_absorption

toolkit = prepare_absorption(
    service,
    content=host_authorized_redacted_transcript,
    title="Project decision conversation",
    provenance=host_capture_provenance,
    reason="Explicit remember request",
)
toolkit.execute_tool("corpus_suggest_create", {
    "document_id": "deployment-preference",
    "title": "Deployment preference",
    "content": "Prefer morning deployments.",
})
pending = toolkit.get_proposal()
```

The pending artifact contains retained transcript evidence and a page citing
that exact evidence revision. There is no filesystem dependency, provider
call or automatic publication. The host reviewer issues one `Review` per
change (matching `document.content_digest`) plus a receipt for the resulting
reviewed proposal. Then call `service.review(pending, reviews)` and
`service.apply(reviewed)`; search immediately reflects the published head.

`CorpusToolkit` implements the existing structural Toolkit interface:
`has_tool`, `execute_tool`, `get_tools_schema`, `render_overview`, and
`get_bound_knowledgebases`. Its twenty-one tools include cited search/get/resolve,
TOC, semantic/hybrid search, path/navigation, legacy hierarchy, strategy/gap reads, and proposed page,
placement, strategy/tree, update and archive changes. There is deliberately
no agent-facing approve/apply tool. A host can invoke the authorized service
write API explicitly. `get_suggestions` returns the JSON proposal artifact;
`clear_suggestions` discards the entire pending batch, including intake.
Start a new absorption after clearing; do not reuse references to discarded
pending evidence.

Pass this toolkit via `extra_tools` to the existing conversation runtime.
For a stored session, `kb_pipelines.absorb_corpus_conversation(session, toolkit,
engine)` uses the host-prepared redacted capture, not implicit raw-session
capture, and returns pending suggestions. It uses the host-selected engine
and existing session store; no new provider or background executor is added.
It supports both existing Claude and OpenAI schema adapters. The old
`absorb_conversation(session, Knowledgebase, engine)` remains unchanged.

Logical corpora are not legacy Knowledgebase rows. Their toolkit intentionally
returns no fabricated legacy `ConversationKBUsage` FK bindings. The runner
now calls its usage hook and binds a cloned service to the session context;
the shared usage API records collection/scope/revision/references without
requiring an Article row. Existing Article FK usage tracking is preserved.

For other intake sources (uploads, coding-agent notes, external records), the
host supplies authorized extracted text and capture provenance to `intake`
or `prepare_absorption`. Credentials, fetching, redaction and binary extraction
belong to host adapters. This is an actual no-filesystem ingestion API, not a
requirement to write temporary YAML or Markdown before using Ergo.

### File-backed authoring and commands

For a Git corpus, stage its authorized snapshot in a writable MemoryCorpus or
DatabaseCorpus workspace, use these same operations, and export the resulting
snapshot. A host publisher can serialize `export["corpus"]` as
`ergo-source.yaml` (inline content is valid) or map content to Markdown paths.
The source and export share the same versioned logical schema; no second wiki
frontmatter schema is inferred. The host must commit/select a new trusted ref
and separately preserve operation history to make staged changes canonical
in Git. The Git adapter itself is read-only and refuses apply; it does NOT
pretend an in-memory apply committed files. Virtual authoring needs no such
publication step: its Memory/DB head is authoritative.

`ergo_corpus ALIAS intake`, `review` and `apply` read bounded JSON from stdin.
Intake accepts the API keyword object with a provenance object. Review accepts
`{"proposal": <proposal envelope>, "reviews": [<review records>]}`; apply accepts
the reviewed proposal envelope. `table_of_contents` and `operations` are also
available as actions. The host alias factory selects the authorized identity
and backend, including a durable workspace if commands run in separate
processes. Reconstructing a fresh MemoryCorpus for every command does not
persist writes. No command accepts an arbitrary principal, Git path or policy.

Run the shared writer/ingestion tests without legacy dependencies:

```text
python -m pytest tests/test_knowledge_corpus.py tests/test_knowledge_writes.py tests/test_knowledge_boundary.py --ds=tests.knowledge_settings -o addopts='' -q
```

The shared matrix covers memory, in-memory SQLite and Git-derived staging for
both example host policies. The legacy/full suite additionally tests real
conversation-session bridging and preserves the existing DB workflows.
