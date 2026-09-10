# Ergo Git release and preservation handoff — 2026-09-10

Primary task: `01a08756-6fcd-7763-8ec0-09bd9f974dbf`.
Worker: `leewardbound/codex-ergo-direction-review`, same devbox checkout throughout.

## Released outcome

The reviewed path-primary implementation is merged and pushed to `main` in
[PR #29](https://github.com/boundcorp/django-ergo/pull/29), merged at
2026-09-10 17:24:52 UTC. The immutable **code-release commit** is:

`566e5aa254fa8064ed035fc062b41d08419d17cf`

Remote: `https://github.com/boundcorp/django-ergo.git`. Local main was fast-forwarded
without changing its original untracked plan. The user selected Git distribution;
no PyPI/TestPyPI upload, release tag, standalone service deployment or consuming-app
migration was performed. This subsequent documentation-only handoff does not change
the selected immutable release identity, even when main advances to include it.

```sh
python -m pip install 'django-ergo @ git+https://github.com/boundcorp/django-ergo.git@566e5aa254fa8064ed035fc062b41d08419d17cf'
python -m pip install 'django-ergo[legacy,filesystem] @ git+https://github.com/boundcorp/django-ergo.git@566e5aa254fa8064ed035fc062b41d08419d17cf'
```

Choose the first for the lightweight common API, or the relevant extras for legacy
Article/conversation and filesystem integration. Hosts still own identity, scopes,
authorization, providers, jobs, UI and explicit Git publication. Virtual paths do
not require files. Existing reviewed moves, history/citations, ingestion, memory,
strategy, lexical/semantic/weighted/hybrid retrieval and usage remain available
through the common API. See `docs/git-release.md` and `docs/knowledge-paths.md`.

## Commits and verification

- Previously reviewed implementation/report: `4f62038`, `fa9c398`; foundation and
  curated fs-vector history `10ec75b`, `fb7b861`, `f666e10`, `cc8b355` are retained.
- `4afcaf8`: replace the broken Docker CI path with isolated pgserver, provider-free
  contract checks, migration drift and an actual installed minimal wheel check.
- `5f744ecd98346667053f8795ce2d146878b21a3e`: verified source/planning preservation
  and Git release instructions. Backup text is marked `-text` for byte preservation.
- `83c1921`: convert validated immutable vectors to lists at the PostgreSQL adapter
  boundary. CI with pgvector 0.5.0 exposed five genuine failures hidden by the older
  local 0.4.2 environment; the regression now asserts the adapter's accepted type.
- `e1d23dd`: configure `django.contrib.postgres` for the legacy example app and
  document it for hosts, satisfying current Django's real search-field checks.
  Remove misleading PyPI quickstart and static coverage claims.

[PR CI](https://github.com/boundcorp/django-ergo/actions/runs/34507507748) passed:
827 passed, 20 skipped; 356 provider-free; no migration drift; minimal wheel passed.
[Code-release main CI](https://github.com/boundcorp/django-ergo/actions/runs/34508072398)
also passed: **827 passed, 20 skipped** in 191.67s, **356 provider-free** in 26.05s,
no migration drift, and installed minimal wheel passed. The full-suite coverage
warning concerns redundant include/source configuration, not a test failure.

Local focused adapter regression: 6 passed using disposable isolated pgserver.
Local migration check: no changes. No prior applied migration file was edited.
Existing Docker developer targets were not rehabilitated; release CI no longer
uses their failing apt-key setup or test-exit-masking command.

An actual fresh Git install, not just an editable source import, was verified at
`/tmp/ergo-git-release-minimal`. Its `direct_url.json` records the exact commit above.
Python 3.12.11 installed only Django 6.1.1, django-environ, asgiref, sqlparse and
django-ergo. No OpenAI, pgvector, psycopg or YAML was installed. Both the installed
virtual command/advanced workflow smoke and additional reviewed subtree move,
strategy/navigation, old citation resolution, rebuilt hybrid retrieval and usage
smoke passed for memory and SQLite. Historical evidence objects are absent from
the wheel. The package version remains 0.1.0; use the full Git SHA in host locks.

## Preservation and completed cleanup

`docs/preservation/2026-09-10/manifest.json` maps **159 source-state records to 63
byte-identical objects**, including all 39 fs-vector originals, all six Cabal plans,
all changed file states from the three detached synthesis commits, and main's
original untracked plan. All records were verified from committed Git objects.
The manifest SHA-256 is:

`20e06d22f83474f1126f166736a295d0fbdb781fa144d430902ce29982228397`

These are inert historical copies, not new implementation commitments or a live
wiki. The June helper research and May 26 reevaluation now have verified Git
backups; attachment metadata was not treated as independently verified backup.
Only allowlisted nonsecret project code/planning was copied. Auth, raw sessions,
runtime databases, `.cabal/codex-home`, environment files and ignored caches/venvs
were not copied. File snapshots do not promise permanent reachability of the old
detached Git commits or live resolution of their historical wiki citations.

**Rigel executed the managed cleanup after main integration**, rechecking all 159
main archive records, all 39 original hashes, expected dirty/ignored inventory,
and both fs-vector TUIs idle. Normal fs-vector removal refused dirty state; the
separately authorized force removal returned `removed:true`. The worker did not
retry or inspect removed checkout paths. A fresh local Git inventory confirms
the same four remaining checkouts reported by Rigel's Orca inventory.

| Path | Result |
| --- | --- |
| `/tmp/django-ergo-synthesis-repo` | Removed by Rigel through Orca; unique historical file states preserved. |
| `/tmp/django-ergo-synthesis-final-repo` | Removed by Rigel through Orca; includes preserved generated wiki states. |
| `/home/dev/orca/workspaces/django-ergo/fs-vector` | Removed by Rigel through Orca after verified preservation and inactivity; its local branch is also absent. |
| `/home/dev/p/boundcorp/django-ergo` | Retained; main release checkout and original untracked plan. |
| `/home/dev/orca/workspaces/django-ergo/codex-ergo-direction-review` | Retained; current active worker. |
| `/home/dev/p/boundcorp/django-ergo/.worktrees/4fe9433fa892` | Retained; planning backed up, excluded operational/auth-state retention unresolved. |
| `/home/dev/p/boundcorp/django-ergo/.worktrees/743a48d58774` | Retained for the same reason; no raw state copied or destroyed. |

The unrelated already-merged `feature/rag-human-feedback-system` ref remains;
its ownership was not established for this cleanup. Both held Cabal refs remain.
No remote source-branch deletion or force push was performed by this worker.

## Adoption boundaries

- Upgrade consumers before publishing corpus v3. Unchanged v1/v2 exports and hash
  receipts remain compatible; assigning paths changes semantics and needs review.
  No hierarchy-code-to-filename guessing or automatic path mapping is performed.
- Legacy hosts need `[legacy]`, PostgreSQL/vector and `django.contrib.postgres` in
  `INSTALLED_APPS`. Lightweight `django_ergo.knowledge` hosts do not need that app,
  PostgreSQL or files. Older Python metadata is not a newly verified support claim.
- `0014_path_primary_articles` removes code uniqueness, but unchanged 0013's
  duplicate-code preflight still applies to older databases. Hosts must explicitly
  reconcile duplicates before traversing it; no fake migrations or destructive
  conversion. A downgrade with duplicate codes is not promised safe.
- Git KB publication remains host-owned and explicit. Memory state is process-local;
  derived indexes need rebuilding after changes. Optional semantic providers/indexes
  are independent of storage choice. No unrelated app or production database was
  migrated. Cabal voice orchestration remains outside this release.

Implementation, Git release verification and authorized cleanup are complete.
The only subsequent work is integrating this documentation-only report through the
same checked PR process; the final terminal handoff records its disposition.
