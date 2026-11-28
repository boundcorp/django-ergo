# Nested Codex init: scope-blocked on installed CLI state layout

Date: 2026-11-27. Task task_679283a48495, dispatch ctx_8fcc5134ad92.

## Disposition

Investigation and bounded no-model reproductions completed; permanent django-ergo fix, regression suite, and draft PR are **not completed**. No repository edits, commits, pushes, credential migration, writable-root changes, config changes, deploys, restarts, or model/media requests were performed. Stop condition: the installed CLI requires writes beneath the existing read-only CODEX_HOME outside the job workspace, and its supported SQLite/log overrides do not relocate all initialization state. The task explicitly requires stopping before a new credential path or policy widening.

## Confirmed baseline

- Fresh worktree `/home/dev/orca/workspaces/django-ergo/codex-nested-init-storage-fix`; HEAD and fetched origin/main both `4e292f3d9d756815bc99dc847412886d25bc6989`.
- Real integration: `src/django_ergo/plugins/local_shell.py`: defaults at 481–484; argv construction at 637–650 preserves `--sandbox workspace-write` plus the existing network setting. Launch uses `env()` at 694, which rebuilds environment from pass_env. Merely exporting a new variable in the worker would not establish inheritance for arbitrary nested CLI invocations unless explicitly wired through the runner.
- `/usr/local/bin/codex` resolves to `/usr/lib/node_modules/@openai/codex/bin/codex.js`; `codex --version`: `codex-cli 0.171.0`.
- Existing effective home path is `/home/dev/.codex`. No auth/config file contents were inspected. Only the CLI's normal opaque initialization could access existing auth/config.
- Read `~/ENVIRONMENT.md` first. No host/service was reported unreachable, so envcheck was not needed. No cluster access was required.

## Matching source and supported controls

Read-only upstream clone at `/tmp/codex-init-source-0171`, tag `rust-v0.171.0`, commit `ccf9be8cc458bd6b8336fe84d68d8893f2691a5e` (tag object `295eca93394bb83a6ec8e0841033f3997f97fcbd`). Source is the official OpenAI Codex repository, https://github.com/openai/codex/tree/rust-v0.171.0.

| Storage | Supported location control / source evidence | Consequence |
| --- | --- | --- |
| State/memories/goals/queue SQLite | `sqlite_home`, fallback `CODEX_SQLITE_HOME`, fallback CODEX_HOME; `core/src/config/mod.rs:4452` | Redirectable inside job; insufficient alone |
| Text and SQLite logs | `log_dir`; `config/src/config_toml.rs:391`, `core/src/config/mod.rs:4472` | Redirectable, with managed-requirement caveat |
| Installation identity | `runtime-agent-instance/src/installation_id.rs:19`: always CODEX_HOME/installation_id; read+write+create open at 24, then shared lock; existing valid content does not avoid writable open | No separate supported path override found; mandatory call in `app-server/src/lib.rs:821` |
| Helper aliases | `arg0/src/lib.rs:345`: CODEX_HOME/tmp/arg0-linux | No separate supported override found; failure warns and continues |
| Session rollouts and index | `rollout/src/lib.rs:88`, `rollout/src/session_index.rs:299`: CODEX_HOME/sessions, archived_sessions, session_index.jsonl and lock | SQLite redirect does not relocate these; handshake cannot validate turn-time writes |
| Models cache | `models-manager/src/manager.rs:382`: CODEX_HOME/models_cache.json | Not a separate supported filesystem setting in inspected config schema |
| Auth/config | `login/src/auth/storage.rs:167`: CODEX_HOME/auth.json; user config remains CODEX_HOME/config.toml | CODEX_HOME relocation also changes auth/config lookup; prohibited migration absent separate approval |

The key new finding versus the previous report is installation_id: a real writable-open dependency independent of SQLite, even if a valid identity file already exists. The current run's stderr was generic EROFS rather than the earlier memories-specific message; no stack trace was collected, so the exact first failing host-home call is not asserted. The source dependency alone prevents a supported all-state-in-job solution using only SQLite/log controls.

## Bounded probes and results

`probe.py` sends **only** initialize, then initialized if it receives a result. It never sends thread/start, turn/start, login, token refresh requests, or media requests. Each nested invocation runs inside `codex sandbox -c 'sandbox_mode="workspace-write"' -c sandbox_workspace_write.network_access=false`; only probe-owned subprocesses are terminated on timeout. No shared daemon is controlled. The script drains stdout/stderr concurrently, so earlier pipeline output handling is not assumed to explain the failure.

1. Existing home, default storage: initialized=false, process exit=1; PATH alias read-only warning and `Error: Read-only file system (os error 30)`.
2. Existing home plus explicit absolute `sqlite_home=<probe workspace>/redirected/sqlite` and `log_dir=<probe workspace>/redirected/logs`: initialized=false, exit=1; same EROFS. SQLite DBs were created at redirected paths. This independently rejects the supported-override-only proposal.
3. Synthetic read-only CODEX_HOME outside the job (contains only a harmless valid UUID installation_id and synthetic credential-sentinel, no auth/config), with state/log paths in job: initialized=false, exit=1, same EROFS. No existing credentials were copied or linked.
4. Synthetic credential-sentinel append under the outer workspace-write sandbox: blocked, errno 30. Opening the already-existing synthetic installation_id with r+ is likewise blocked, errno 30. No real auth/config write was attempted.
5. Empty writable **unauthenticated** synthetic home inside job, same outer sandbox and network disabled: initialize result=true, exit=0. This proves the installed CLI can handshake under the outer sandbox if all its home state is writable; it does **not** establish preservation of subscription auth and is not a proposed migration.

Probe files: `probe.py`, `job/negative.py`; synthetic inputs at `synthetic-home/`; generated disposable probe state beneath `job/` and the per-case folders. All are outside the repository. No symlinks are used. The successful control is deliberately unauthenticated and cannot stand in for the requested authenticated production route. Existing-home runs were bounded at 25 seconds plus shutdown and failed; there is no successful authenticated handshake evidence.

## Exact narrow next-step proposal (not executed)

Preferred permanent direction: add or obtain an upstream-supported runtime-state directory distinct from CODEX_HOME, then wire it into an opt-in local_shell launch without relocating auth/config. Proposed per-run root is `<job>/.ergo/codex-state/run-<N>/` and only that directory; sqlite, log, installation_id, helper aliases, rollouts/index, caches, and other mutable runtime artifacts must all honor it. Keep `/home/dev/.codex/auth.json`, `/home/dev/.codex/config.toml`, hooks, rules, and plugin configuration read-only at their existing paths; no copied credentials, symlinks, token environment extraction, or new auth home. This requires a CLI change/version support before a django-ergo-only implementation can claim success.

A narrower **initialization-only** policy exception would at least need `/home/dev/.codex/installation_id` writable in addition to redirected SQLite/logs; it is not implemented or recommended as a complete fix because it shares identity state, leaves subsequent home writes unresolved, and violates the requested all-runtime-state-in-job property. Broadening ~/.codex, an auth symlink, bypass flags, or a Claude switch are not acceptable substitutes.

Future regression requirements once a supported runtime-state split exists: unique physical directories per job/run; reject symlinked roots/components; prevent environment overrides of the selected state root; preserve the default sandbox and current auth/config lookup; synthetic auth/config writes fail; no credentials enter snapshots/specs/artifacts; consecutive and concurrent jobs never share writable state; free handshake under outer workspace-write and no turn/model request. Actual authenticated model execution and refresh cannot be validated within the present no-model/no-credential-change scope; read-only auth refresh remains a distinct possible limitation.

No draft PR was opened because no safe working repository implementation was found; presenting SQLite/log overrides as a fix would misrepresent the reproduced failure. The next coordinator decision is whether to authorize upstream CLI work (same read-only auth/config boundary) or separately approve a precise alternative; this report grants neither.
