# PR #417 security review: analysis container-log isolation (B7) and images (B4)

Independent adversarial review of `leewardbound/analysis-images-logs`, starting
from head `278d7eb`. I audited the code itself rather than the PR description:
`src/django_ergo/analysis/executor.py`, the controller check in
`kubernetes.py`, `infra/analysis/*` and `.github/workflows/analysis-images.yml`.
The feature stays default-off. Nothing was deployed, merged or restarted.

## Verdict

With the fixes below it is safe to merge (default-off). The core design holds
up: a non-dumpable PID 1 holds the only log descriptor, user code gets only
pipes, the runtime probe fails closed, every process is killed before the
single record, and the termination-message digest is checked. The one real
gap was that untrusted files were parsed inside PID 1. That gap is now closed.

## Findings

### Medium: untrusted file parsing ran in PID 1 with the log descriptor open (fixed)

`dataset`/`select` ran `pandas.read_csv/read_parquet/read_excel` (pyarrow,
openpyxl) on workspace files in the executor process itself. User Python
writes those files in an earlier pod. A memory-corruption bug in a parser
would therefore run attacker code in the one process that holds the container
stdout, with no FSIZE limit. Those ops also skipped the isolation probe. The
author listed this as a residual risk in the plan doc.

Fix: `parse_in_child()` forks before any parsing. The child closes `LOG_FD`,
calls `setsid()`, applies `child_limits()` (FSIZE and core) and stays
non-dumpable, because dumpability is inherited across fork. It returns JSON
over a pipe capped at `RECORD_BYTES`. Its errors are dropped. The parent
enforces a 55 s deadline, SIGKILLs the child, reaps everything and requires
exit status 0. The probe now gates parsing too.

Regression tests (`tests/test_analysis_log_isolation.py`):
- `test_compromised_parser_cannot_reach_container_log` and its `_as_pid1`
  variant replace the parser with the full escape probe.
- `test_real_parse_runs_in_child_and_returns_result`
- `test_parser_failure_is_generic`
- `test_parse_refuses_without_log_isolation`

The image smoke test gained a `dataset-parse` case, so the fork path also runs
under runc and gVisor in CI.

### Low: the fail-closed probe passed on any `OSError` (fixed)

The probe exited 0 (treated as "isolated") on any error, including ENOENT from
a missing `/proc` or a wrong pid. Those errors prove nothing. It now passes
only on EACCES/EPERM. CI logs show that both runc and gVisor 20261102.0 return
EACCES, so this change keeps working on both. Test:
`test_probe_fails_closed_on_non_permission_errors`.

### Low: same-namespace code could raise KeyboardInterrupt in PID 1 (fixed)

Python installs a SIGINT handler by default. PID 1 ignores signals it has no
handler for, but not SIGINT, so user code could call `os.kill(1, SIGINT)` and
abort the executor at an arbitrary point. I found no route from this to a log
write, because all writes happen after kill-all and an aborted executor writes
nothing. It did let user code kill its own job and skip the termination
rewrite. `main()` now ignores SIGINT, and `child_limits()` restores SIG_DFL in
children. Test: `test_sigint_cannot_interrupt_pid1`.

### Low: workflow actions pinned by tag; checkout persisted the token (fixed)

Every `uses:` now pins a commit SHA, with the version in a trailing comment
(checkout v4.4.0, setup-python v5.6.0, setup-buildx v3.12.0, login v3.7.0,
build-push v6.19.2). Both checkouts set `persist-credentials: false`. Before
this, PR code (Dockerfiles, smoke test) on a same-repo branch could read the
`packages: write` token from `.git/config`. `uv` is pinned to 0.13.10.
`test_workflow_pins_actions_and_drops_checkout_token` enforces the SHA pins,
`persist-credentials`, publishing only from main, no `pull_request_target`,
and no pipe-to-shell.

### Coverage gap: ptrace and pidfd_getfd were claimed but not tested (fixed)

The PR said ptrace and `pidfd_getfd` fail, but the escape probe only tried
`/proc` opens. `log_escape_probe.py` now tries `PTRACE_SEIZE` on the parent and
`pidfd_open` + `pidfd_getfd` on fds 0–31. The local tests and the runc/gVisor
smoke test assert that both are blocked.

## Checked, no issue found

- **Reopening stdout/stderr:**
  - `/proc/1/fd/*`, `fdinfo`, `mem` and `environ` give EACCES (non-dumpable PID 1, `CAP_SYS_PTRACE` dropped).
  - `/dev/stdout`, `/dev/stderr`, `/proc/self/fd/1` and `/dev/fd/1` resolve to the child's own bounded pipe.
  - `/dev/tty` gives ENXIO (new session); `/dev/console` gives ENOENT (no tty in the pod).
  - Inherited fds: none (`close_fds`). The saved descriptor is close-on-exec and opened only after `closerange`.
- **Fork before hide:** no process exists before `isolate_log_stream()` runs. The probe and all children are forked afterwards. Dumpability never changes again, because the executor never execs or changes credentials.
- **FSIZE:** the hard limit equals the soft limit, so a process without `CAP_SYS_RESOURCE` cannot raise it. Grandchildren inherit it. SIGXFSZ is ignored, so writes past the limit fail with EFBIG. This caps the host-backed termination file at 64 MiB.
- **Kill-all:** `kill(-1)` from PID 1 reaches every process in the PID namespace, including subreapers, setsid daemons and nested namespaces, because they are all visible from the parent namespace. The waitpid loop runs until ECHILD. Under RuntimeDefault seccomp without CAP_SYS_ADMIN, code cannot leave the namespace. `shareProcessNamespace` is unset and the pod has one container.
- **Termination message:** the executor writes it with O_TRUNC after kill-all. The controller requires exactly `{v,bytes,sha256}` and a constant-time hash match against the log. To forge a record, user code would need a log write as well, which is the thing being prevented. If the executor dies before rewriting the file, the log is empty or invalid and is rejected.
- **Workflow:**
  - Triggers are `pull_request` only, and the registry login and push are gated to push on main.
  - The GHA cache is written only on publish, which prevents cache poisoning from PRs.
  - Locks are installed with `--require-hashes --only-binary=:all:`, and bases are digest-pinned, which both the Dockerfiles and the workflow check.
  - gVisor is downloaded with `curl -o`, verified with `sha512sum -c` and never piped to a shell.

## Residual risks

- A kernel or gVisor bug that defeats dumpability or ptrace checks would reopen the log. The controller's digest check would reject the record, but node disk is still consumed until kubelet rotation.
- User code can still fill its own bounded outputs: the 64 MiB termination file on host disk until pod deletion, the workspace PVC quota and the `/tmp` tmpfs.
- `images` holds `packages: write` on PR runs too. The token is no longer persisted to disk, but a stricter split would put the push in a separate job that runs only on main. Fork PRs get a read-only token regardless.
- The `test` job still installs `pytest` and `httpx` without hashes. That job has no secrets and only `contents: read`.
- infra-octo admission still has to pin `terminationMessagePath`/`terminationMessagePolicy` (noted in the plan doc, tracked in #337).
