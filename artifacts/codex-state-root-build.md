# Codex runtime-state root: built and tested fix

2026-11-27. Task `task_ed4f2f20fad6`, dispatch `ctx_db25236217b7`. Continues
`artifacts/codex-init-review.md` and `artifacts/codex-init/REPORT.md`.

## Result

A patched Codex CLI 0.171.0 was **built from official source** and **passed
35/35 offline end-to-end checks**. Under the real outer `workspace-write`
sandbox with networking disabled, its `app-server` completed the `initialize`
handshake with a **read-only synthetic `CODEX_HOME`**. An inotify audit
recorded **zero write events** in that home, even when the home was left
writable. The unpatched build of the same commit still fails with `EROFS`, as
reported. Without the new variable, the patched build writes exactly the same
paths as the baseline.

Draft PR: https://github.com/boundcorp/django-ergo/pull/419.

The django-ergo opt-in (`local_shell` `codex_runtime_home: true`) is
implemented and tested. It is a draft PR only, and nothing is enabled or
deployed. Turning it on in Ergonaut needs a reviewed, pinned build of this patch
(or the change landing upstream) at `executables.codex`.

## What changed upstream (`codex-state-root/runtime-home.patch`)

There is a new opt-in variable, `CODEX_RUNTIME_HOME`. It is proposed here and is
not an existing upstream setting. When it is set, Codex-generated mutable state
is rooted there. `auth.json`, `config.toml`, `.env`, rules, hooks, user skills,
AGENTS.md and the plugin configuration are still read from `CODEX_HOME`. Nothing
is copied, symlinked or extracted.

- `utils/home-dir/src/runtime_home.rs`: validates the variable once per process
  (`OnceLock`). The root must be an existing absolute directory. It must have no
  symlinked component and no `..`, must not be group- or world-writable, and must
  not overlap `CODEX_HOME` in either direction. It is never created by Codex.
  An invalid value is an error. Writers use `try_*`. Infallible readers get an
  uncreatable `/proc/self/fdinfo/...` placeholder, so the code can never fall
  back to `CODEX_HOME`.
- `arg0`: `arg0_dispatch_or_else` rejects an invalid root before any work.
  Helper aliases (`tmp/arg0-linux/*`) go to the runtime root, and core's
  helper-dir recognition matches.
- `runtime-agent-instance`: `installation_id` is per runtime root. This was
  the mandatory write that blocked init.
- `core` config: the default `sqlite_home` and `log_dir` come from the runtime
  root. With a runtime root, any `sqlite_home`/`log_dir` outside it (from config,
  `CODEX_SQLITE_HOME` or managed requirements) is **refused** rather than
  silently overridden. Memories root and the V1 detection use the runtime root.
- `core` `RolloutConfigView for Config::codex_home()` returns the runtime
  root. That one seam routes rollout and thread-store paths: sessions,
  archived_sessions, session_index.jsonl and its lock, writer locks, compression
  and `.tmp` maintenance markers, and rollout migration.
- Shell snapshots and their cleanup, `history.jsonl`, `models_cache.json`,
  memories read/write/control/ext prompts, `skills/.system` install and lookup,
  plugin cache/data, curated plugin sync repo/sha/lock, npm and remote-bundle
  staging, share local-path metadata, cloud-config bundle and managed-config
  caches, default worktrees root, and the exec-server plugin-cache discovery
  root.

Size: 35 files, +117/−40. The patch excludes `Cargo.lock`, because cargo
regenerates it offline. The only non-version change in the lock is 10 new
`codex-utils-home-dir` edges. The tag's own lock lists workspace crates as
`0.0.0` against `Cargo.toml` `0.171.0`, so `--locked` fails even without this
patch.

## Pins (auditable)

| Item | Pin |
|---|---|
| Source | https://github.com/openai/codex `rust-v0.171.0` = `ccf9be8cc458bd6b8336fe84d68d8893f2691a5e` (same as installed CLI) |
| Patch | `artifacts/codex-state-root/runtime-home.patch` sha256 `506c1e8b…d722e` |
| Toolchain | rustc 1.99.1 (58cf8f9c3 2026-10-13), cargo 1.99.1, from `rust-toolchain.toml`, installed under the build dir only |
| rusty_v8 | Codex release `rusty-v8-v152.2.0`, `ptrcomp_sandbox_release` x86_64 linux. Manifest sha256 `1ec73063…9350` matched the in-repo trusted manifest; archive sha256 `b3b490f2…dbcd` |
| OpenSSL | Debian `libssl-dev_3.0.22-1~deb12u2_amd64.deb` sha256 `c5f948bf…c90e`, matched against the bookworm-security `Packages` index (InRelease signature not checked). Linked statically |
| Baseline binary | `bin/codex-0.171.0-baseline` sha256 `73c2a4c7…9c44` |
| Candidate binary | `bin/codex-0.171.0-runtime-home` sha256 `f0c39daa…5dec4` (debug profile, no debuginfo) |

Reproduce: `sh artifacts/codex-state-root/build.sh`, then `e2e.py` (see the
script header). The build directory is `build/codex-state-root/` (git-ignored,
about 6 GB), with `-j2` throughout. Baseline took 14m27s and the incremental
candidate 8m14s on the shared devbox.

## End-to-end regressions (`codex-state-root/e2e.py`, results in `e2e-results.json`)

Every Codex child runs inside the installed CLI's
`codex sandbox -c sandbox_mode="workspace-write" -c sandbox_workspace_write.network_access=false`.
Only `initialize` and then `initialized` are sent: no thread, turn, login,
refresh or model request. Homes are synthetic, holding sentinel
`auth.json`/`config.toml`. A recursive inotify watcher outside the sandbox
logs every create/modify/attrib/move/delete event under each home.

| Check | Result |
|---|---|
| Baseline, writable home: initializes; writes `installation_id`, 5 SQLite families, `skills/.system/**` and `tmp/arg0-linux/**` into CODEX_HOME | PASS |
| Baseline, read-only home: fails (the bug) | PASS |
| Patched, no opt-in: initializes, writes the **same normalized path set** as baseline; with a read-only home it still fails | PASS ×3 |
| Patched + runtime root, read-only home: initializes; **0 home events**; sentinels intact; sandbox denies auth/config writes; `installation_id` in root | PASS ×5 |
| Patched + runtime root, **writable** home: **0 home events** (EROFS is not just hiding attempted writes) | PASS ×2 |
| Consecutive runs: distinct installation ids, no shared inodes | PASS ×2 |
| 3 concurrent runs: all initialize, distinct ids, 0 home events | PASS ×3 |
| Rejected, fails closed with 0 home events: symlinked root, symlinked ancestor, root inside CODEX_HOME, root == CODEX_HOME, relative, missing (not created); symlink target untouched | PASS ×14 |
| `sqlite_home` / `log_dir` override outside root refused, nothing written there | PASS ×2 |
| Read-only home unchanged across all cases | PASS |

Total: **35 passed, 0 failed.** The first run showed 34/35. The one failure was
in the harness: random `codex-arg0XXXXXX` names, and inotify directory events
that depend on timing. Path normalization fixed it, and the code under test did
not change.

Upstream unit tests (`cargo test -j2`, patched tree, variable unset, so these
are the default-behaviour regressions): codex-utils-home-dir 13 (including the
new resolver tests), codex-arg0 8, codex-cloud-config 48,
codex-message-history 12, codex-runtime-agent-instance 28 (including
installation_id), codex-skills 54, codex-worktree 5+3+24. That is **195
passed, 0 failed** in 13m05s. The `codex-core` and `codex-core-plugins`
suites were not run: their test builds are the largest in the workspace, and
the e2e run covers their init-time paths.

## django-ergo opt-in (draft PR)

- `local_shell.py`: `codex_runtime_home` (bool, default false, type-checked).
  For Codex runs only, `launch()` creates `.ergo/codex-state/run-<n>/`
  exclusively with mode 0700. It rejects a symlinked `.ergo`/`codex-state` and a
  reused run number, and passes the resolved path as `CODEX_RUNTIME_HOME`.
  `CODEX_RUNTIME_HOME` is in `NEVER_PASS`, so an inherited or `pass_env` value
  never reaches any job, even with the option off. State stays under `.ergo`, out
  of `_snapshot` and the file report. Existing `--sandbox` and network flags,
  `CODEX_HOME` and `HOME` are unchanged.
- Tests: 4 new tests. `tests/test_local_shell.py` passes 27/27. Ruff check and
  format are clean.
- Docs: `docs/bots.md` (key and behaviour) and `TODO.md` (open item).

## Limits and what is not claimed

- **Auth refresh with a read-only home is unsupported.** Refresh writes
  `auth.json` and creates `auth.lock` in CODEX_HOME. Under the sandbox those
  writes fail with EROFS, so an expired token fails the run (fail closed). Auth
  was deliberately not redirected, so no credentials ever enter job state. Long
  jobs need either a token fresh enough for the run or a separate,
  explicitly approved auth-writer decision. No token was read to assess expiry.
- Turn-time writers (rollouts, history, snapshots, memories, model and plugin
  caches) are routed in code, but no turn ran, because that would need a model
  request. Only initialization is proven end to end.
- The migration was found by static search and the init-time write audit. It is
  not a whole-program syscall proof. Paths not migrated in this patch include the
  TUI (`visualizations`, `ipc`, update cache), the app-server daemon/control
  socket and managed-install state, CLI `doctor`/`migrate-rollouts` readers,
  Windows sandbox state, external-agent migration and the session-import ledger.
  The stdio app-server and `codex exec` paths that local_shell uses do not
  reach them in the audited flow. Under the sandbox, any missed writer fails
  loudly with EROFS rather than sharing state.
- Runtime mode starts with an empty plugin cache and an empty curated-plugin
  snapshot. Plugins installed into the shared home are not visible in jobs.
- Root validation is path-based, and the launcher owns a fresh private
  directory. It is not race-proof against a concurrent local attacker. The outer
  sandbox remains the protection for CODEX_HOME.
- A fresh `installation_id` per run changes analytics and remote-registration
  identity semantics. Upstream review is needed, and none of this was exercised.
- This is a debug build for testing, not a release artifact. No complete upstream
  crate suites, clippy, Bazel or release builds were run.

## Safety record

`envcheck` ran and reported all SSH routes reachable. Nothing was reported
unreachable. No real auth or config was read, copied or linked. No model, paid or
media request was made, and the probes ran with network disabled. The patched
binary was not installed anywhere and was run only from the build dir. There was
no merge, deploy, restart, cluster or infrastructure change, and no change to
secrets or permissions.

There was one side effect, which has been reverted. `make pip_install`
(`uv pip install -e .`) installed `django-ergo` into the active shared
`~/.config/python` env. It was not there before, and no other packages changed.
I uninstalled it, confirmed the import fails there again, and installed into
the worktree `.venv` with an explicit `--python` instead. `pre-commit install`
also (re)wrote `.git/hooks/pre-commit` in the shared repo's git dir. That is
the repo's documented hook, and I did not check whether it was already
present.
