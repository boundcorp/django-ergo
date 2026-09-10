# Git release and adoption

The selected distribution channel is Git. There is no PyPI/TestPyPI upload, release
tag requirement, or standalone Ergo service deployment. A release is an immutable
commit integrated into `main` after passing the repository's `Tests` PR workflow.
Verify the post-merge run too. Do not use an earlier masked Docker success as test
evidence: the repaired workflow executes pytest directly with isolated pgserver,
checks migration drift, tests provider-free contracts and checks an installed wheel.

## Install

Replace `FULL_COMMIT_SHA` with the verified 40-character main commit in the release
handoff, then lock that reference in the consuming application's dependency file:

```sh
python -m pip install 'django-ergo @ git+https://github.com/boundcorp/django-ergo.git@FULL_COMMIT_SHA'
python -m pip install 'django-ergo[legacy,filesystem] @ git+https://github.com/boundcorp/django-ergo.git@FULL_COMMIT_SHA'
```

The first command supports common memory/SQLite/database KB integration without
OpenAI, vector, PostgreSQL or YAML dependencies. The second is for hosts using the
legacy Article/conversation app and optional filesystem services. Choose only the
extras the host actually uses; `[openai]` is independently available. Hosts select
their Django version and providers. The verified runtime is Python 3.12; legacy
package metadata advertising older interpreters is not a newly tested support claim.
The distribution version remains 0.1.0; use the Git SHA, not that unchanged version,
as the immutable release identity. Reinstall/update host locks explicitly.

## Adopt deliberately

- Upgrade readers before publishing `ergo-corpus/v3`. V1/v2 input and unchanged
  historical digests remain compatible; assigning a path requires a new reviewed
  revision. Never infer filenames from hierarchy codes.
- Hosts using `django_ergo.knowledge` alone use its independent migrations and may
  use SQLite or memory without a filesystem-backed KB. The legacy `django_ergo`
  app still needs PostgreSQL/vector and `[legacy]` dependencies.
- Applied migrations are unchanged. `0014_path_primary_articles` drops legacy-code
  uniqueness without rewriting IDs, content or paths. An older database must still
  satisfy unchanged 0013's duplicate-code preflight. Resolve that explicitly before
  upgrading; do not fake migrations or guess a code-to-path mapping. Downgrade with
  duplicate codes is not promised safe.
- Hosts own authorization, scope, UI, jobs, providers, Git publication and all actual
  application/database rollouts. No consuming app is automatically migrated by this
  repository release. A reviewed logical move does not implicitly rename or commit
  physical files. Rebuild derived indexes after content/path changes.

See [paths](knowledge-paths.md), [common integration](knowledge-foundation.md), and
[filesystem integration](fs-vector-integration.md) for the implemented APIs and
explicit compatibility boundaries.

## Historical planning

`docs/preservation/2026-09-10/` preserves allowlisted source evidence with SHA-256
checksums. These objects are inert historical copies, not executable migrations,
tests, an active wiki, or commitments to implement every old plan. They are not
copied into the wheel. Originals remain until their separate preservation and
Orca-managed inactivity/removal checks are satisfied.
