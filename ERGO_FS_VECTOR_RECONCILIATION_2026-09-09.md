# Ergo fs-vector reconciliation — 2026-09-09

Primary task: `01a08756-6fcd-7763-8ec0-09bd9f974dbf`.
Destination: `leewardbound/codex-ergo-direction-review`.
Worktree: `/home/dev/orca/workspaces/django-ergo/codex-ergo-direction-review`.

## Outcome and commits

The backend-neutral foundation is committed. The committed fs-vector branch is
merged locally; its much larger **uncommitted** implementation is separately
curated into this destination. This is not a claim that merging its branch alone
included its code. No source checkout, held worktree, original planning document,
production database, deployment or remote branch was changed.

- Foundation: `10ec75b299cbf585774d1807ceb6b341646dd939`.
- Source ref: `leewardbound/fs-vector` at
  `5f3e5deed73edce9e28898749d3e0f2041488a34`.
- Local merge: `fb7b86128848dc7e7051413eee6b69c72d39e574`.
- Curated integration commit: `f666e10d9388f5300038f6808466b66324571b00`.
- Current execution status: integration and validation complete; not continuing.
  No deployment or push. This report is committed separately after the code.

The source branch differs from the common starting commit
`0e9c9c2bae2cba5ea3de7f23cb8a97edb7181bbd` by one committed configuration
change: `.ergo/index.yaml` and `.ergo/wiki-goals.yaml`. Its source checkout has
12 modified tracked files and 27 untracked files. The complete read-only
inventory/checksums are below. No AGENTS.md was found in the worktree hierarchy
or inspected source tree.

## Deliberate reconciliation

| Concern | Integrated result |
| --- | --- |
| Common API parity | Existing CorpusService memory/DB/Git validation, cited retrieval, intake/review/apply, history, hierarchy/strategy, semantic and weighted/hybrid search, usage sinks, toolkit and commands remain. Optional repository code is not imported by this core. |
| File identity versus hierarchy | Retain optional Article.relative_path and path-prefix lookup, **not** fs-vector's replacement of hierarchy lookup/TOC/order. Legacy hierarchy codes and uniqueness remain; managed source-only rows may use NULL. Common identities never require paths. |
| Embeddings | Retain automatic unmanaged Article embeddings, contrary to fs-vector's global auto_embed=False change. Managed sync skips implicit providers and explicitly invalidates stale vectors; index_article/index_articles remain available. Common indexes/providers are independent of storage. |
| Retrieval ownership and lifecycle | Retain scoped querysets and user-owned garden search. Article visibility combines its lifecycle with managed-source state/status. Missing/archived/draft/stale/superseded projections are excluded. Common import cannot resurrect missing projections. |
| Projection | Curate KnowledgeSource/SourceDocument, committed reads, UUID reconciliation, missing tombstones, checkpoints and managed instance/admin guards. Fix frontmatter lifecycle mapping and persist prior_paths after rename. Invalid statuses fail before publication. |
| Repository indexing/search | Retain Python/Markdown extraction, source files/units/relations, reconciliation, fingerprints and PostgreSQL index schema. Add explicit provider-free lexical indexing/search and lexical RepositoryToolkit behavior; optional semantic/hybrid mode retains fingerprint checking. |
| Portable bundle/citations | Retain builder/validator/commands and reviewed page preservation. Fix both current-path membership checks for ancestor citations; validate captured Git blob IDs as well as SHA-256/length. Reject bundle symlinks. |
| Wiki synthesis | Retain read/grep/outline/history/relation tools, inspected-evidence checks, goal coverage, proposals and review packets. Retain source coverage configuration, correcting stale pipeline paths and adding common-API coverage. No synthesis run against originals. |
| Canonical format | Common v1/v2 records remain the authored contract. Legacy frontmatter is input; kb.yaml is a portable export inventory, not competing canonical truth. read_wiki_records + propose_wiki_import explicitly translate through retained capture, host review and canonical export. Virtual hosts pass parsed records without YAML/files. |
| Conversation compatibility | Curate source fixes moving synchronous ORM reconstruction/tool execution off async event loops, retaining the foundation's logical usage hook and legacy conversation APIs. |
| Packaging | Keep base Django/django-environ only. YAML is an optional adapter dependency with actionable import errors. Legacy app still requires its declared PostgreSQL/vector/OpenAI extra; no applied migration is rewritten to evade that requirement. |

Source README prose is represented by corrected integration docs rather than
blindly replacing the destination README. The source test-settings repository
alias is **not** copied: tests retain isolated pgserver configuration, and hosts
must provision aliases deliberately. The source garden filter duplicates our
already-correct owner-scoped implementation. Source review reports and workflow
JSON remain in place and are not staged or executed. Old plans are evidence, not
implementation commitments.

## Actual workflow/API parity

| Workflow | Common public entrypoint, all logical backends | Integration/legacy evidence |
| --- | --- | --- |
| Ingestion and explicit memory | intake, prepare_absorption, CorpusToolkit pending captures, absorb_corpus_conversation | test_knowledge_writes, test_knowledge_pipeline; old Article absorption/toolkits retained |
| New wiki intake | propose_wiki_import accepts parsed records; all data passes normal review/apply | test_knowledge_wiki_import: 2 host policies × memory/DB/Git staging, file/process traps, denial before consuming input, archived exclusion |
| Writes/review/correction/withdrawal | propose, revise, review, apply, operations; exact proposal receipts and revision CAS | Shared writes suite; Git remains explicit staging/export/host commit, not an implicit writer |
| Organization | create_page, get_by_hierarchy, by_hierarchy_prefix, get_strategy, propose_strategy/tree, get_tree_status | Shared advanced-feature tests; legacy hierarchy callers preserved rather than redirected to paths |
| Retrieval | lexical, semantic content/summary, vector, multi-field weights, hybrid, cited revision resolution | Shared advanced matrix, denied access, scope isolation, archived exclusion, stale projections and fingerprints |
| Usage | MemoryUsageStore, DatabaseUsageStore, FileUsageStore independent of source; with_usage_context | Three readers × three sinks × two host policies; actual conversation usage hook and old FK path |
| API/tool integration | CorpusService, 15 common toolkit tools, ergo_corpus management command, host factories | Both host policies, both tool adapters, existing conversation runner |
| Repository-specific evidence acquisition | Optional Git/AST/history/source-unit adapter; resulting records can enter any common corpus | Source adapter tests + portable-bundle-to-common review test; these operations inherently need source material, but do not impose Git on virtual KB authoring/search/review |
| Existing Article clients | Original models, QuerySets, toolkit APIs and embedding behavior; explicit ArticleCompatibility bridge | Legacy suite plus reviewed import/publication, source fingerprint concurrency and preserved identities |

A parsed wiki capture cites the **retained original page record**, including its
legacy metadata. It does not claim independent verification of every old unit
ID/claim. Host-resolved underlying evidence can be admitted and linked through
the existing common proposal API. Neither an old reviewed label nor a Git commit
automatically grants common publication.

Two consuming-app fixtures remain executable in `examples/knowledge_hosts.py`:
engineering project membership/review and maintenance company/site membership/
supervision. Their identities, source admission, authorization, providers, jobs
and UI are host-owned, not Ergo tenant models.

## Migration graph and supported starts

```text
0009 -> 0010_article_status -------------------------------+
     -> 0010_knowledgesource_sourcedocument_... -> 0011_... -+
                                                           -> 0012_merge_corpus_and_sources
                                                           -> 0013_reconcile_article_compatibility
```

The two 0010 files touch different fields and do not create a duplicate status
column. Source 0010 and 0011 were copied **byte-identically**, preserving their
names/dependencies; existing 0000–0009 and foundation status-0010 are unchanged.
Independent knowledge-app 0001–0003 are unchanged.

0013 restores hierarchy ordering, indexes and uniqueness while retaining
optional path uniqueness/nullability and source/index models. No content, IDs,
paths or codes are rewritten. MigrationExecutor tests cover starts at 0009,
foundation 0010, source 0010 and source 0011; tests preserve Article content/IDs
and source checkpoint/unit data. Fresh isolated test DBs traverse the full graph.

**Genuine adoption decision:** fs-vector permitted duplicate non-null hierarchy
codes. If a host has such rows, 0013 fails with an actionable message rather than
inventing assignments or deleting data. The host chooses intended codes (or
explicit NULL for source-only rows), then retries. A regression verifies the
failed migration leaves both conflicting rows intact. This condition was tested
in isolation; no actual host database was inspected or altered. Downgrading a
nullable-source database to the old non-null hierarchy schema is not promised.

## Validation evidence

- First curated adapter/legacy/migration run: **40 passed**.
- First full reconciliation run: **756 passed, 20 skipped, 2 failed**. The two
  new ancestor-path regressions exposed a second validator gate; it is corrected,
  not skipped or weakened to accept uncaptured references.
- Corrected targeted migration/citation/index/wiki/import run: **23 passed**.
- Provider-free memory/SQLite/shared-backend suite: **293 passed**.
- Final full isolated pgserver suite: **760 passed, 20 skipped**, 209.63 seconds.
  Migration drift check: **no changes** in django_ergo or ergo_knowledge.
- Full configured Ruff passes for common knowledge modules and new integration/
  migration/wiki-import tests. Optional repository modules pass syntax/import/
  undefined-name checks (E4/E7/E9/F/I), and the selected 32 files pass formatting.
  No repository-wide all-rule lint claim: salvaged legacy modules retain
  exception/style/complexity diagnostics outside those checks.
- Latest wheel rebuilt locally and installed into
  `/tmp/ergo-direction-minimal`. Its only distributions are Django,
  django-environ, asgiref, sqlparse and django-ergo. Imported package path was
  verified inside that environment, not the source checkout.
- Installed-wheel smoke passes advanced memory/SQLite reviewed intake, hierarchy,
  strategy, weighted/semantic/hybrid retrieval, cited resolution, operations,
  usage and parsed wiki intake, with no pgvector/psycopg/OpenAI/YAML installed.
- Wheel: `/tmp/ergo-direction-wheel/django_ergo-0.1.0-py3-none-any.whl`;
  SHA-256 `0edfd80087996058604bd086d89b3fcc08ae8d1174c76636b022ef9c085156f5`.
  This is a local validation artifact, not a published release.

Full test command (temporary pgserver, no shared database):

```bash
env -u DATABASE_URL -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u TEST_OPENAI \
  ERGO_TEST_ISOLATED=1 PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH=/tmp/ergo-direction-review-test-deps:src \
  /home/dev/p/boundcorp/cabal/.venv/bin/python -m pytest tests -q --tb=short \
  -o addopts='' --reuse-db --ds=tests.example_app.settings
```

The shared interpreter is read-only; optional test-only jose dependencies live
under /tmp. Every isolated settings process creates its own temporary server.
The reuse-db flag only avoids async teardown warnings within that temporary
instance; it does not select a shared application DB.

## Limitations and adoption actions

1. Install appropriate extras and enable only the apps a host needs. Existing
   legacy PostgreSQL/vector migration requirements remain; common virtual installs
   are independently verified without them.
2. Hosts must select source/destination aliases, scopes, admission/redaction,
   reviewers, provider fingerprints, usage persistence, jobs and publication refs.
   Common MemoryCorpus/vector/default usage are process-local, not durable ANN.
3. Optional persisted repository vectors remain 1536D by historical schema.
   Lexical-only repository mode avoids that requirement; common vectors permit
   host dimensions for every backend. No missing-capability lexical fallback is
   disguised as successful semantic retrieval.
4. GitCorpus is a committed reader. Stage changes, review/apply, export, then
   explicitly publish through a host-controlled Git writer; nothing here silently
   commits repositories. Source-managed Article publication stays source-owned.
5. Article/common import is explicit capture, not automatic bidirectional deletion
   reconciliation. Canonical operation logs and independent usage sinks are not
   a distributed transaction; inspect committed-error state before retry.
6. Optional repository tools/ORM access require prior host authorization. Model
   guards do not stop privileged bulk SQL. Portable bundles are internally
   consistent evidence, not signed authenticity or proof of claim support.
7. Resolve duplicate legacy hierarchy codes deliberately if a host upgrade reports
   them. No faked migrations, automatic reassignments or source worktree changes.
8. No push, deployment, PR merge, source-branch deletion or Cabal integration was
   performed. Cabal voice/project orchestration remains outside implementation.

Implementation/use documentation: `docs/knowledge-foundation.md` and
`docs/fs-vector-integration.md`. Previous milestone reports describe their
historical checkpoints; this report is the integration handoff.

## Preservation verification

Main retains only its original untracked `docs/plan-fs.md`. Both Cabal worktrees
remain held. All six planning SHA-256 values match the original preservation
report, including the June and May-26 documents without verified backups.
The original direction memo still hashes to
`1cb682c5a5ff19ca74f1ab53f063d83aa85b2d269e5c34a20b11b330fffdaec3`.
No auth files, raw sessions, .cabal/codex-home or existing databases were copied.
Final fs-vector status and all 39 source-file hashes match the inventory below.
Its original branch remains at 5f3e5de; it is an ancestor of the destination HEAD.

## Read-only fs-vector inventory

Paths are relative to
`/home/dev/orca/workspaces/django-ergo/fs-vector`.
This lists the source checkout's uncommitted state; it is **not** a destination
staging list. Original review reports and workflow JSON are excluded from the
implementation commits.

```text
 M .ergo/wiki-goals.yaml
 M README.md
 M pyproject.toml
 M src/django_ergo/admin.py
 M src/django_ergo/conversation/engines/claude_api.py
 M src/django_ergo/conversation/engines/openai_api.py
 M src/django_ergo/conversation/runner.py
 M src/django_ergo/embedding_providers.py
 M src/django_ergo/kb_tools.py
 M src/django_ergo/models.py
 M src/django_ergo/settings.py
 M tests/example_app/settings.py
?? CABAL_WORKTREE_PLANNING_PRESERVATION_REVIEW.md
?? ERGO_FLAT_FILE_FOUNDATION_DECISION_MEMO.md
?? FILESYSTEM_KB_SNAPSHOT_CITATIONS_REVIEW.md
?? django-ergo-self-index-direction.workflow.json
?? src/django_ergo/filesystem_kb.py
?? src/django_ergo/git_snapshot.py
?? src/django_ergo/indexing.py
?? src/django_ergo/management/commands/build_repository_kb.py
?? src/django_ergo/management/commands/index_repository.py
?? src/django_ergo/management/commands/propose_repository_wiki.py
?? src/django_ergo/management/commands/sync_knowledge.py
?? src/django_ergo/management/commands/validate_filesystem_kb.py
?? src/django_ergo/migrations/0010_knowledgesource_sourcedocument_alter_article_options_and_more.py
?? src/django_ergo/migrations/0011_knowledgesource_index_config_hash_and_more.py
?? src/django_ergo/paths.py
?? src/django_ergo/repository_index.py
?? src/django_ergo/repository_search.py
?? src/django_ergo/repository_wiki.py
?? src/django_ergo/repository_wiki_prompt.py
?? src/django_ergo/sync.py
?? tests/test_filesystem_kb.py
?? tests/test_repository_index.py
?? tests/test_repository_reconciliation.py
?? tests/test_repository_search.py
?? tests/test_repository_wiki.py
?? tests/test_repository_wiki_prompt.py
?? tests/test_sync.py
```

SHA-256 of those 39 source files (used only for preservation verification):

```text
0a0b557510a8e15f19e80dd19dd21327c4dca8ff9522da8d626a32796f6d9c7d  .ergo/wiki-goals.yaml
443bccd3f8788601bf054c1ec42590061f89b0b238a644a4ebad9097d8b7cee4  README.md
e9830513effc46ea9469331bb0f0bf0f8bf97c11dc809c7c5d63b61739dd9734  pyproject.toml
7db7004a359e86d3be277848b60494ea8f37e14446ccf273ad39c7513dc40cbd  src/django_ergo/admin.py
0fef77cda32056c0001fe8aff7bdb99cce4034059377a8461cc625a96501e1f0  src/django_ergo/conversation/engines/claude_api.py
e5d4efe5b634f02c320adbef4c881f383f8d4c56b0cf87745cdffd7634ff7e31  src/django_ergo/conversation/engines/openai_api.py
81ed13ae18a4388c4043cbcc310cfcf3f45b6716093fa1c8d2449ea159cf91c4  src/django_ergo/conversation/runner.py
d318b312c63a32f94cd7f9c30121263eb6aeb729c84e39ba82d678b54e87c24d  src/django_ergo/embedding_providers.py
23d09796f282abd9299f0f9c2fe910fd161ee33f9919dcad9302dc83384632b7  src/django_ergo/kb_tools.py
fca96ef125fd776064c01692229ecde1124483b4de076bffd5df558dbeddd0a8  src/django_ergo/models.py
5af786160ece2d1e59dfa708ad918cddb3ddc12452dbf569890342c18e4d51ca  src/django_ergo/settings.py
7e0d310a9956ef36af22f233091edab896e7f3281bd3576a69f19e187e8780d4  tests/example_app/settings.py
d78d7d598e6115fd22be037c1724c19633e85eec444a6c4628b8b820f1cbdd1a  CABAL_WORKTREE_PLANNING_PRESERVATION_REVIEW.md
45db843c2d3113998f7d4d804e1e8b99cdc278535ae6fc14eb5e389f57933588  ERGO_FLAT_FILE_FOUNDATION_DECISION_MEMO.md
1ac8f2daa36e3c8be6998533c63f20a3e59bf613139532a7f039777b0fc35faa  FILESYSTEM_KB_SNAPSHOT_CITATIONS_REVIEW.md
60c108f199f8ba41d767e18d62f338e1ccff7446ebd28502c26a29e69066eae3  django-ergo-self-index-direction.workflow.json
bf18f72ee3e91e098be40c6768226796138f98af9763c2d25f8d2a348f4de42e  src/django_ergo/filesystem_kb.py
0add794f14043edf4dda93a6cbf5245bc0e740197f5503d9234723998483d92e  src/django_ergo/git_snapshot.py
2c83191ba131c2661ea9ecf4e88cc1f768ac970a1c4e10411ad490138ad4105a  src/django_ergo/indexing.py
9fded1d29f7085c3be0942eaa6f5cd02e346a897c7968888751476f3e6d02f4d  src/django_ergo/management/commands/build_repository_kb.py
7b7b8c828eb390681059c979621a6bfdabbaee033b0ecea4db9023f472be3932  src/django_ergo/management/commands/index_repository.py
67baaec4b9010a2f8a2aab96fbbaaf81117205c6c63fc871207eb582ab5daea1  src/django_ergo/management/commands/propose_repository_wiki.py
308124ecf6583ca9921aa991fe1b2e4f23f58ed9ae73f0d66414487215a7051f  src/django_ergo/management/commands/sync_knowledge.py
a67ff7f1b4bc9667145bb3b8d5634ef2b64f741e89bd8480d42adeb8f99de094  src/django_ergo/management/commands/validate_filesystem_kb.py
980508c446ec1c9d09bda1454044970b38fd769ef15bfa2aa86c7fe610c06f83  src/django_ergo/migrations/0010_knowledgesource_sourcedocument_alter_article_options_and_more.py
7c5d6a0cc34fd99e1acf90e49bde69828325627e217947495ffcc6baf9fd725b  src/django_ergo/migrations/0011_knowledgesource_index_config_hash_and_more.py
ee0b6d9c65d4694d0fa150a4ce6e8445da137ffcf7cc6a84e9e0698b96882686  src/django_ergo/paths.py
6e5dce6b2a6add8798e5e0865bb09c858a224961baecfd5f28f5e756a97701c5  src/django_ergo/repository_index.py
db864a2e81cb674b6db55c56ac0c3360877eabc2a7d3a1c89ee4863cf7a7a8af  src/django_ergo/repository_search.py
d6eea9e38df6f3727908420f83101720175d593dc0b9cf0008f4feeb9e02a303  src/django_ergo/repository_wiki.py
8f5185f52bd14eb58defa837ecc0718660880114f6d4346c127e32b6e7aec53d  src/django_ergo/repository_wiki_prompt.py
dd6149be7f1619997929f61d68acd2cdafe2bac3bdc65e8bde654a86548c6775  src/django_ergo/sync.py
e5396f627bf7dce3843715e035e6ed1edacc854e989c4dd65207d45acad7aa6f  tests/test_filesystem_kb.py
ad59d45482e21e4a7708e7daa09855256ae58f0b6dd5ddd3bd4e4769e51127b0  tests/test_repository_index.py
f21a3f81c5085ef95eaf2944d2d0014031bc60937c937699528ab8345cb42c1a  tests/test_repository_reconciliation.py
61e461ac1422b9ca0eefaa447952738898c447afc5adcfb2ff9d9aa7ded6307d  tests/test_repository_search.py
fd2c268fc1cd657b6d67a48b68cb2de2b562985a22ece39f519478cc5866290a  tests/test_repository_wiki.py
ffbd92d922926ab2ab537711fc28be3a66a8fc75682de99d1f0914a31dd9a8dc  tests/test_repository_wiki_prompt.py
d38b09c5035cd2a45032f8159aecb1413a9f3769ec5d660ba9696bd78c675d58  tests/test_sync.py
```
