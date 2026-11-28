# Codex nested startup: implementable source route and bounded evidence

2026-11-27. Task `task_7792f2d07873`, dispatch `ctx_4bcd25aa6ced`.

## Result

A concrete proposed upstream patch, source-level regression harness and full
build/acceptance recipe now exist in this worktree. The patch removes the known
mandatory shared-home identity write dependency and redirects helper aliases to
a proposed separate runtime directory. The patched identity function succeeded
under the actual outer sandbox while synthetic auth/config/old identity writes
were denied. **The permanent production fix is not complete:** the complete
runtime-path migration, full Codex build and patched app-server handshake have
not run. No django-ergo launch behavior was changed or enabled.

The supervisor-requested sanitized predecessor report was copied unchanged to
`artifacts/codex-init-review.md`. This report supersedes its claim that no changes
exist, while retaining its reproduction history separately.

## Artifacts

- `startup-runtime-home.patch`: seven upstream files including new resolver,
  Cargo dependency/lockfile, identity writer, helper creation and core helper
  recognition. Applies cleanly to the matching public source.
- `runtime_home.rs`: identical resolver module extracted for convenient review;
  the test harness checks byte equality with the module created by the patch.
- `check_patch.py`: offline compile, ten resolver tests, four separate job IDs,
  eight concurrent same-root identity readers, synthetic-home digest checks,
  and kernel sandbox positive/negative test.
- `handshake.py`: reusable synthetic-only app-server handshake acceptance probe,
  including a future `--binary ... --expect-patched` mode.
- `evidence.json`: sanitized machine-readable results; no fixture UUIDs, home
  contents, authentication tokens or raw CLI diagnostics are retained.
- `BUILD-AND-ACCEPTANCE.md`: exact build commands and integration gates.

## Source and upgrade check

Installed CLI remains `codex-cli 0.171.0`. Matching official source is
[OpenAI Codex rust-v0.171.0](https://github.com/openai/codex/tree/ccf9be8cc458bd6b8336fe84d68d8893f2691a5e),
commit `ccf9be8cc458bd6b8336fe84d68d8893f2691a5e`. The existing public clone
`/tmp/codex-init-source-0171` was read without changing its working files.
Read-only public Git tag discovery found 0.172.0 as the greatest stable semantic
version returned, commit `3d86e43b075adeba2193ec5c22ac92f55afd82e6`. Its
[identity implementation](https://github.com/openai/codex/blob/3d86e43b075adeba2193ec5c22ac92f55afd82e6/codex-rs/runtime-agent-instance/src/installation_id.rs)
still uses `CODEX_HOME/installation_id` and read+write+create open. Its inspected
home-dir resolver and configuration schema still expose CODEX_HOME plus
SQLite/log controls, without the proposed runtime root. **An upgrade alone to
0.172.0 is not a supported solution to this dependency.** This is a source check,
not an execution test of 0.172.0 or a claim about every future release.

## Proposed patch behavior

`CODEX_RUNTIME_HOME` is a new proposed upstream opt-in, **not an existing official
setting**. With it absent, existing home behavior is retained. When present, the
root must already exist, be absolute, have no symlink components, and not overlap
the canonical config home in either direction. An invalid root errors instead of
falling back to a writable shared home. No root is created by validation.

The identity function now accepts `&Path`, uses the runtime root for its existing
locked UUID persistence algorithm, and keeps its previous locking/normalization
logic. Existing callers using AbsolutePathBuf coerce to Path; the full workspace
still needs compilation. Both helper creation and core helper recognition use
the same resolver. The arg0 `.env` reader stays at original CODEX_HOME. SQLite
and log paths use existing explicit overrides during the probes. There is no
configuration copy, auth-home replacement, credential symlink or binary patch.

This is intentionally the **startup slice**, not all-state containment. Root
validation uses path-based metadata and is not race-proof against a concurrent
filesystem adversary. The launcher must own a newly created private directory;
the outer sandbox remains the protection for original auth/config. Descendant
symlink handling, resolving once per launch, and complete writer migration must
be reviewed before any production enablement. Fresh per-run identity also needs
upstream review for installation/remote-runtime registration semantics; no remote
registration was tested or requested.

## Mutation-path matrix and remaining work

Paths below are relative to original CODEX_HOME unless noted. References are
relative to `codex-rs/` at the matching SHA. Static search included direct home
joins, find_codex_home consumers, home/cache/temp APIs, and subsystem
constructors. It is a discovered mutation matrix, **not an exhaustive syscall
proof**; inline test code and indirect path plumbing prevent a simple search
from establishing completeness. No production all-path containment claim is made.

| Mutable family | Source evidence | Current control / required migration |
| --- | --- | --- |
| installation_id; file permissions and locks | runtime-agent-instance/src/installation_id.rs:19; app-server/src/lib.rs:821; app-server/src/in_process.rs:484 | Proposed patch relocates the common writer, including in-process/other callers. |
| SQLite state, memories/goals/queue DBs and diagnostic DB/WAL/SHM | core/src/config/mod.rs:4452 | Existing sqlite_home/CODEX_SQLITE_HOME; full root mode must reject out-of-root overrides/managed conflicts. |
| Text and SQLite logs | core/src/config/mod.rs:4447–4473 | Existing log_dir; managed sqlite_home can govern SQLite logs separately. |
| tmp/arg0-OS helpers, janitor dirs and locks | arg0/src/lib.rs:320–390; core/src/config/permissions.rs:53 | Proposed patch routes creation and helper recognition; upstream arg0 suite not run. |
| sessions, archived_sessions, compressed rollouts, session_index.jsonl + lock, thread-writer-locks, migration journals, .tmp maintenance | rollout/src/{recorder,session_index,writer_lock,compression,maintenance}.rs; thread-store/src/local/{archive_thread,delete_thread,rollout_migration}.rs | Move both readers and writers via ThreadStoreConfig/runtime paths. SQLite alone does not route files. |
| history.jsonl append/trim/locking | message-history/src/lib.rs:70–124 | Pass runtime home to MessageHistoryConfig. |
| shell_snapshots and temporary snapshot files/cleanup | core/src/shell_snapshot.rs:225–282,1139 | Route creation and rollout-based cleanup together. |
| memories, memories_v2, memories_extensions, resource/control files | core/src/config/mod.rs:3803–3810; memories/{read,write}/src/lib.rs; memories/write/src/control.rs | Read/write roots must agree; DB routing is insufficient. |
| models_cache.json | models-manager/src/manager.rs:380–382,925 | Route ModelCatalogSettings disk cache to runtime root; leave model configuration source intact. |
| skills/.system and fingerprint marker | skills/src/lib.rs:66–102 | Route embedded installation/cache to runtime; retain custom user skill roots read-only. |
| plugins cache/data, installed marketplaces, staging, .tmp/plugins sync repo/SHA/locks, share local-path metadata | core-plugins/src/{store,startup_sync,installed_marketplaces,npm_source,remote_bundle}.rs and remote/ | Split immutable configured roots from generated caches/data; cannot blanket-replace codex_home because loaders also edit config. |
| cloud-config-bundle-cache.json, managed-config-NAMESPACE.json and atomic replacements | cloud-config/src/cache.rs:43–51,170 | Runtime cache root; managed policy inputs remain at their original read-only sources. |
| app-server-control sockets; app-server-daemon PID/settings/locks; managed packages and update state | app-server-transport/src/transport/mod.rs:56–78; app-server-daemon/src/{lib,managed_install}.rs | Stdio jobs should not launch/update shared daemon; route supported local runtime state or explicitly reject those operations. |
| worktrees and editor scratch | worktree/src/settings.rs:44; tui/src/external_editor.rs | Runtime root or explicitly scoped workspace location; TUI is outside handshake acceptance. |
| auth.json + auth.lock + temporary atomic replacements; OAuth .credentials.json, secrets/gateway_oauth.json, other secrets, MCP OAuth locks | login/src/auth/storage.rs:167,195–206,274; login/src/oauth_session_storage.rs:26,95; rmcp-client/src/{oauth,oauth/store_lock,oauth/enterprise_generation}.rs; secrets/src/local.rs:207 | **Do not redirect these into job state.** Preserve read-only source and reject persistence/refresh/login when a write is required. No credential copy/migration proposed. |
| config.toml/profile files, rules/default.rules + migration marker, hooks/agents config and environment configuration edits | core/src/config/edit.rs; core/src/exec_policy.rs:966; execpolicy/src/sandbox_migration.rs:21; external-agent-migration/src/service.rs; exec-server/src/environment_toml.rs | Keep original reads; deny explicit edits/migrations in this mode, rather than treating config as cache. |
| OS temp/cache and personal plugin checkout roots outside CODEX_HOME | core-plugins/src/marketplace_policy.rs:557–582; core-plugins/src/remote/share/checkout.rs:66; exec-server/src/environment.rs:1288 | Audit generated writes separately; explicit runtime cache/temp routing or rejection. Changing HOME wholesale would alter configuration discovery. |
| hook/MCP/plugin subprocess side effects and arbitrary tool output | Configured executable inputs and workspace tools | Outer sandbox still governs these. A path-root refactor cannot itself guarantee arbitrary child filesystem behavior. |

Therefore a job-local root can contain the selected Codex-generated runtime
artifacts **after** the constructor migration and conflict checks above. It cannot
contain auth/config mutation while simultaneously retaining those files read-only:
those operations must fail closed, or require a separate explicit credential or
policy decision. Token freshness and authenticated model operation are not
established by this work. No need to widen ~/.codex permissions to develop or test
the source split.

## Executed checks and limitations

1. Patch applies to matching source, including Cargo.lock; source clone working
   tree remained unchanged.
2. Ten std-only Rust resolver tests passed: default behavior, sentinel preservation,
   relative/empty/missing/file/traversal rejection, overlapping roots, root and
   ancestor symlinks, distinct physical job roots.
3. The exact proposed identity source compiled with cached real Tokio/UUID crates
   using installed Rust 1.90.0. Four concurrent separate roots produced four IDs;
   eight concurrent same-root calls reused the same ID. Synthetic home hashes
   were unchanged. This harness is explicitly not a full crate/workspace build.
4. Under installed Codex workspace-write with network disabled, patched identity
   creation succeeded inside the job, while writes to synthetic auth.json,
   config.toml and original installation_id outside the job failed. No real home
   or credential contents were supplied.
5. Installed 0.171.0 app-server: read-only synthetic home + job-local SQLite/logs
   + proposed variable still failed initialization with EROFS; the variable is
   ignored by the unpatched CLI. Writable unauthenticated synthetic control
   initialized successfully and exited 0. Both used network-disabled sandbox and
   initialize/initialized only. Read-only-home sentinel hashes were unchanged.
6. Python probes pass repository Ruff formatting/lint checks. Full Django suite
   was not needed because no production Django code changed.

Full upstream compilation requires its pinned Rust 1.99.1 toolchain; no toolchain
installation was attempted. The proposed patch's complete upstream unit suites,
Bazel dependency integration, full app-server linkage and patched handshake are
unverified. No claim that a source-level module test is a complete CLI fix.

`~/ENVIRONMENT.md` was read first. No host/service was reported unreachable and no
cluster route was needed. An attempted `envcheck` returned
`/usr/bin/bash: line 1: envcheck: command not found` (exit 127); this does not imply
any host is unreachable. Public Git fetch/tag routes worked. No runtime, cluster,
shared binary, credentials, permissions policy, merge, deploy or restart changed;
only branch artifacts and disposable synthetic fixtures/builds were written.

## Exact proposed destination and next step

Recommended route: complete and review the upstream runtime-path split, build a
candidate as a branch artifact, then opt in django-ergo's existing local_shell
runner to `<job>/.ergo/codex-state/run-<N>/` per run. Keep source auth/config at
`/home/dev/.codex/auth.json` and `/home/dev/.codex/config.toml`, read-only and
unmoved. Proposed destinations for installation_id, tmp, SQLite/logs, sessions,
indexes, snapshots, memories, model/plugin/skill/cloud caches and runtime sockets
are children of that job root, preserving their relative layout where suitable.
Set no writable exception on `/home/dev/.codex` or any of its files.

No credential/policy operation is required to continue the source work. If future
auth-refresh testing requires persistent writes, the exact affected source is
`/home/dev/.codex/auth.json` plus `/home/dev/.codex/auth.lock` and same-directory
atomic replacement files; **no writable target or credential migration is proposed
or executed here**. A precise alternative would require a new review decision,
not an implicit fallback to a credential-bearing job home.
