# Proposed upstream build and acceptance recipe

This is a review recipe, **not a deployed fix**. `startup-runtime-home.patch` is
an implementable first slice against OpenAI Codex `rust-v0.171.0`, commit
`ccf9be8cc458bd6b8336fe84d68d8893f2691a5e`. The new `CODEX_RUNTIME_HOME` variable
is proposed here; installed Codex does not support it. Do not enable it in
Ergonaut until the complete runtime split and full build pass review.

## Reproduce completed checks

From this django-ergo worktree, with a public source checkout at the given SHA:

```sh
python3 artifacts/codex-init/check_patch.py /path/to/public/codex
python3 artifacts/codex-init/handshake.py
```

The first script applies the patch only in a disposable worktree-local tree,
uses cached dependencies (`cargo +stable build --offline`), compiles the exact
proposed production identity module, and runs it in a network-disabled Codex
sandbox. It does not compile the full upstream workspace. It aliases the
`codex_utils_home_dir` crate to the exact proposed runtime-home module in a small
harness; it uses real Tokio and UUID dependencies. Ten std-only Rust tests run
against that same resolver module. No wrapper substitutes the identity algorithm.
The second script expects the installed unpatched CLI to fail with read-only
synthetic CODEX_HOME and succeed with writable unauthenticated synthetic home.
Both CLIs receive a minimal environment and synthetic homes only.

## Full upstream build (not run here)

Use a separate source checkout and a build worker with the repository's pinned
Rust 1.99.1 toolchain and normal build dependencies. This session's installed
stable compiler is 1.90.0; no toolchain installation or shared CLI replacement
was attempted. Run in that checkout, replacing the absolute patch path:

```sh
git checkout -b fix/separate-runtime-home ccf9be8cc458bd6b8336fe84d68d8893f2691a5e
git apply --check /absolute/django-ergo/artifacts/codex-init/startup-runtime-home.patch
git apply /absolute/django-ergo/artifacts/codex-init/startup-runtime-home.patch
cd codex-rs
cargo fmt --all -- --check
cargo test --locked -p codex-utils-home-dir
cargo test --locked -p codex-runtime-agent-instance installation_id
cargo test --locked -p codex-arg0
cargo test --locked -p codex-core active_arg0_helper_dir
cargo build --locked -p codex-cli
```

`Cargo.lock` includes the added home-dir dependency. Upstream formatting, feature
unification, Bazel integration and the complete crate suites have **not** been
validated by the isolated harness. Existing identity tests must also run with
`CODEX_RUNTIME_HOME` absent. Add full app-server integration coverage with each
fixture root passed only to the subprocess; do not mutate process-global
variables in parallel Rust tests.

From the django-ergo worktree, run the full candidate binary only in the supplied
synthetic handshake harness:

```sh
python3 artifacts/codex-init/handshake.py --binary /path/to/codex-rs/target/debug/codex --expect-patched
```

This sends `initialize` and then `initialized` on success, never a turn, thread,
login or refresh request. It uses the installed CLI solely to apply the outer
workspace-write, network-disabled sandbox. It never installs the candidate.
A passing result is initialization acceptance only.

## Required second slice before recommending production

Introduce a resolved `RuntimePaths` value (config root plus separate runtime
root) into Config and the relevant subsystem constructors. Resolve once per
launch; do not keep resolving a mutable process environment in individual
consumers as the first slice does. Use the mutation matrix in REPORT.md as the
migration checklist. Pass runtime home to both readers and writers of session
state. Keep auth, user config, rules, custom skills and plugin configuration
readers attached to original CODEX_HOME. Keep embedded skill installation,
plugin downloads/cache and sync metadata attached to runtime home; compose the
read-only user roots with the runtime roots without copying configuration.

When runtime home is selected, reject any SQLite/log/runtime override or exact
managed requirement outside the runtime root with an actionable error. Never
silently defeat managed requirements. Reject config/auth mutation RPCs before
performing filesystem writes, and fail closed on auth refresh requiring a
persistent write. A read-only auth storage wrapper must reject write-lock
creation as well as save/delete/compare-and-save; preventing only auth.json
replacement leaves auth.lock behind. No credentials belong in runtime state.

## Proposed django-ergo integration, after CLI acceptance

In `src/django_ergo/plugins/local_shell.py`, create one fresh physical 0700 root
`<folder>/.ergo/codex-state/run-<number>/` before launch, using exclusive creation
and rejecting symlink components. Add only that resolved runtime root to the
runner environment built by `env()`; do not rely on the coordinator shell's
exports. Pin the approved candidate executable through existing executables
configuration. Keep current CODEX_HOME and workspace-write/network flags. Prevent
pass_env from overriding the selected root. Exclude state/cache contents from
snapshots, specs and artifacts; logs/rollouts can contain task text even when
they contain no credential files. Never fall back to shared home on failure.

Acceptance additionally needs consecutive/concurrent run isolation, denied
synthetic auth/config writes, override and symlink rejection, constructor-level
filesystem tests for every row of the mutation matrix, and an offline syscall
write audit of initialization. A no-model handshake cannot establish turn-time
behavior. Offline writer tests can establish storage routing without paid calls;
actual authenticated model/refresh behavior remains a separately scoped test.
No merge, deployment, restart, shared executable replacement or credential
operation is part of this recipe's execution in this session.
