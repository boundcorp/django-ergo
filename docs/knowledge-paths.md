# Path-primary knowledge, including virtual KBs

`Document.path` is the primary organization key for common knowledge pages.
It is a logical string, not a filesystem locator. The same schema, validation,
navigation, moves, search, review and usage APIs work for memory, Django JSON
storage and committed Git corpora. Hosts still own scopes, identity, permissions,
providers, jobs and publication. No Git/path-on-disk dependency enters virtual KBs.

## Contract

- Nonempty paths are relative, case-sensitive, NFC Unicode strings, at most
  1024 characters. Reject absolute paths, empty/dot/traversal segments, leading
  or trailing segment whitespace, control characters, backslashes, colons,
  percent/query/fragment markers and backticks. Validation rejects rather than
  silently normalizing. Names/extensions come from hosts, never hierarchy codes.
- A path is unique among current page heads within one collection/scope,
  including archived/draft/stale/superseded reservations. Other collections can
  use identical paths. Evidence/strategy records have no navigation path.
- Prefixes use segment boundaries: `manuals` includes `manuals/pump.md`, not
  `manuals-old/pump.md`. Directories are implicit; a logical page node can also
  have children. This does not imply a physical file can be a directory.
- Empty path means **unmapped compatibility data**. It stays retrievable by ID,
  cited search and unrestricted TOC, but is not placed in path navigation's
  directory list, prefix results or path-tree counts. No filename is inferred.
- Moving releases the old head path for deliberate reuse; it does not create a
  redirect. Path lookup returns current active heads. Historical citations use
  stable document ID, revision and digest, never a mutable path as identity.

## Common service

```python
proposal = service.create_page(
    document_id="pump-guide",
    path="maintenance/pumps/reset.md",
    title="Reset the pump", content=host_reviewed_text,
    sources=(admitted_evidence_reference,), provenance=host_provenance,
    reason="Place the admitted guide",
)
```

Alternatively pass `parent_path="maintenance/pumps", name="reset.md"`.
The name must be one explicit segment. Parent directories need no DB object
or filesystem directory. Do not mix this with legacy parent-code allocation.
Creation returns a proposal. A host supplies reviews bound to each changed
document digest and an exact proposal receipt; then `review`/`apply` publishes.

Reads: `get_by_path(path)`, `by_path_prefix(prefix)`,
`table_of_contents(prefix=...)`, `navigation(prefix)` (directories and pages).
Results include logical `path`, legacy `hierarchy_code` metadata, and stable
citations. Search ranking/providers are unchanged and storage-independent.

Writes: `move(document_id, new_path, reason=...)` or
`revise(document_id, path=..., reason=...)` appends a reviewed revision.
`move_tree(prefix, destination, reason=...)` moves every matching page head,
including archived reservations, in one proposal. It revises explicit path-tree
headings in the strategy in the same batch; it never rewrites arbitrary prose,
embedded links, old evidence or historical revisions. Subtree self-moves,
destination collisions and duplicate strategy-tree paths fail. Explicit bulk
`propose` can atomically swap paths without changing IDs.

`propose_tree("maintenance/pumps", ...)` writes a heading of the form
`### Path tree \`maintenance/pumps\`: Pumps`. `get_tree_status()` returns
`[{"path": "maintenance/pumps", "article_count": ...}]` for active mapped pages.
Strategy remains reviewed Markdown. Historical `Tree #A:` plans are not silently
converted: read them as content or call `get_tree_status(legacy=True)` and
`propose_legacy_tree` explicitly. Duplicate path-tree headings are invalid.

All writes require normal history/evidence/propose/review/apply permissions.
Scope cannot change through a move. Canonical revisions invalidate derived
semantic indexes; rebuild explicitly. Review receipts bind paths, so an approval
for one destination cannot be reused for another. Old citations and usage records
continue resolving the old logical revision.

## Toolkit and commands

The shared toolkit adds `corpus_paths`, `corpus_get_path`, `corpus_navigation`,
`corpus_suggest_path_page`, `corpus_suggest_move`, `corpus_suggest_move_tree`.
Its existing tree tool now takes a logical path prefix. None can approve/apply.
Legacy code and unmapped-create tools remain explicitly labeled compatibility
entrypoints; preferred agent instructions should use the path tools.

`ergo_corpus ALIAS paths --prefix maintenance`, `get_path --path ...`,
`navigation --prefix ...` and TOC use the same host-bound service.
`create_page`, `move`, `move_tree`, `propose_tree` consume JSON stdin and return
reviewable proposals. A create payload uses serialized provenance and source
references; a move payload uses `document_id`, `path`, `reason`. Read/search,
semantic/hybrid modes and usage commands remain available.

## Article compatibility and mapping

Article `relative_path` now represents the same logical primary path; blank
remains unmapped. `ArticleCompatibility.propose_import` uses existing paths or an
explicit `path_mapping={str(article.pk): "chosen/name.md"}`. The mapping is keyed
by stable source IDs; invalid/foreign IDs or unsafe/colliding paths fail before
publication. It never converts `A1` to `A/1` or invents names from titles.
Import is reviewed capture, not a migration that alters the source DB.

`publish` accepts code-free mapped pages, preserves UUIDs, checks source
fingerprints/locks, retains automatic unmanaged Article embeddings, and enforces
path conflicts against untouched rows. Simultaneous mapped path swaps are
transactional: temporary path clearing is invisible outside the transaction,
and failures roll back. Managed source Articles remain read-only; publish their
source instead. Missing source projections remain excluded when imported.

Article ordering and `Knowledgebase.get_table_of_contents()` are path-primary;
unmapped rows display an explicit ID marker, not a synthesized filename.
`get_legacy_table_of_contents()` preserves the old code view. Existing code
query/tool APIs remain for old callers, but duplicate code lookups are inherently
ambiguous and must use ID/path or explicit host mapping. Common
`get_by_hierarchy` fails clearly on ambiguity; listing by code remains possible.
Code allocation is retained only for callers explicitly requesting the old code
arguments; these pages remain unmapped. Code uniqueness is not a path invariant.

## Schema and upgrade boundaries

`ergo-corpus/v3` carries nonempty logical paths (and allows duplicate legacy code
metadata). The reader still accepts v1/v2. Empty added fields are omitted from
canonical hashes: unchanged old document digests, exports and proposal receipts
retain their exact bytes/hash semantics. Tests compare v2 golden hashes and
proposal receipts obtained from the pre-path installed wheel, plus earlier v1
goldens. Assigning a path is a semantic revision requiring fresh review, not a
free upgrade of an old approval. Older readers cannot safely read v3; upgrade
consumers first. Path fields under v1/v2 declarations are rejected.

Additive migration `0014_path_primary_articles` removes hierarchy uniqueness and
changes ordering/help text, retaining path uniqueness and all existing IDs,
content, codes and blank paths. No prior migration is modified. The independent
knowledge-app migrations are unchanged because paths live in canonical JSON.

**Historical upgrade caveat:** a host before 0013 must still satisfy that
unchanged migration's code-uniqueness preflight before reaching 0014. Hosts already
at 0013 can directly upgrade to 0014. If an earlier fs-vector database has duplicate
codes, obtain an explicit host mapping/choice (for example NULL for genuinely
source-only legacy codes), retain the old mapping as host evidence, then proceed.
Do not fake applied records or silently rewrite codes. This prior precondition
cannot be erased without changing applied history. No real host DB was inspected
or altered. Downgrade to 0013 with duplicate codes is not supported without host
reconciliation; normal forward upgrades retain those codes after 0014.

Existing invalid source paths require explicit host corrections/mapping; the
migration does not guess normalized names or create files. Strict validation
applies to new common revisions and Article saves, not privileged raw SQL.

## Explicit Git publication

GitCorpus reads committed v1/v2/v3 authored records. A logical path and the
adapter's `content_path` are different: the latter merely locates content bytes.
Stage/review/apply in memory or DB, export, and let the host publisher explicitly
write/commit an authored manifest and content, then select the trusted ref.
A logical move alone does not rename a physical file or commit Git. Virtual
paths work identically without either. The optional legacy portable-wiki importer
uses actual wiki-relative paths as explicit input; parsed virtual records supply
their own path and require neither YAML nor filesystem access.
