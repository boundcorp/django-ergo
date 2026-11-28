#!/usr/bin/env python3
# ruff: noqa: S603, T201, PLR2004, PLR0913, PLR0915, PT017 -- standalone bounded regression harness
"""Offline end-to-end regressions for a CODEX_RUNTIME_HOME-patched Codex build.

Every Codex app-server child runs inside the *installed* CLI's
`codex sandbox` with workspace-write and network access disabled, so nothing can
reach a model, login or refresh endpoint. Only `initialize` (and `initialized`
on success) is sent. All homes are synthetic fixtures holding sentinel
auth.json/config.toml files; no real credentials or configuration are read,
copied or linked. The candidate binary is executed from its build directory and
never installed.

A recursive inotify watcher, running outside the sandbox, records every
create/modify/attrib/move/delete event under each synthetic CODEX_HOME. That
catches writes that a before/after hash diff would miss (temporary files, lock
files, attempts that were created and removed).
"""

import argparse
import asyncio
import contextlib
import errno
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from inotify_audit import WriteAudit  # noqa: E402

SENTINELS = {
    "auth.json": '{"OPENAI_API_KEY":null,"synthetic":"SENTINEL-AUTH-NOT-A-TOKEN"}\n',
    "config.toml": '# synthetic fixture\nmodel = "synthetic-sentinel-model"\n',
}


def clean_env(home: Path, runtime: Path | None = None) -> dict:
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "CODEX_HOME": str(home),
        "LANG": "C.UTF-8",
        "RUST_LOG": "warn",
    }
    if runtime is not None:
        env["CODEX_RUNTIME_HOME"] = str(runtime)
    return env


# --------------------------------------------------------------------------
# Inner side: runs inside the outer sandbox.
# --------------------------------------------------------------------------


async def inner(binary: Path, home: Path, runtime: str, extra: list[str]) -> dict:
    blocked = []
    # Probe only homes outside the job: an append-open on a writable home would
    # itself register as a write event in the audit.
    if not home.resolve().is_relative_to(Path.cwd().resolve()):
        for name in SENTINELS:
            try:
                with (home / name).open("a"):
                    pass
            except OSError as error:
                assert error.errno in (errno.EROFS, errno.EACCES, errno.EPERM)
                blocked.append(name)
    env = clean_env(home, Path(runtime) if runtime != "-" else None)
    p = await asyncio.create_subprocess_exec(
        str(binary),
        *extra,
        "app-server",
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    stderr_task = asyncio.create_task(p.stderr.read())
    initialized = False
    user_agent = None
    try:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "synthetic-init-only", "version": "0"}},
        }
        p.stdin.write((json.dumps(request) + "\n").encode())
        await p.stdin.drain()

        async def response():
            while line := await p.stdout.readline():
                message = json.loads(line)
                if message.get("id") == 1:
                    return message
            return {}

        with contextlib.suppress(TimeoutError):
            message = await asyncio.wait_for(response(), timeout=30)
            initialized = "result" in message
            if initialized:
                user_agent = message["result"].get("userAgent")
                p.stdin.write(b'{"jsonrpc":"2.0","method":"initialized"}\n')
                await p.stdin.drain()
                await asyncio.sleep(1.0)
    finally:
        p.stdin.close()
        stdout_task = asyncio.create_task(p.stdout.read())
        try:
            await asyncio.wait_for(p.wait(), timeout=5)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(p.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(p.wait(), timeout=3)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(p.pid, signal.SIGKILL)
                await p.wait()
    await stdout_task
    diagnostics = (await stderr_task).decode(errors="replace")
    return {
        "initialized": initialized,
        "exit": p.returncode,
        "user_agent": user_agent,
        "read_only_error": "Read-only file system" in diagnostics,
        "runtime_home_error": "CODEX_RUNTIME_HOME" in diagnostics,
        # Last diagnostic line only; fixtures contain no secrets.
        "stderr_tail": diagnostics.strip().splitlines()[-1:] if diagnostics else [],
        "sentinel_writes_blocked": blocked,
    }


# --------------------------------------------------------------------------
# Outer side: fixtures, sandbox launch, inotify audit, assertions.
# --------------------------------------------------------------------------


def digest_tree(root: Path) -> dict:
    out = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            out[rel] = "symlink:" + str(path.readlink())
        elif path.is_file():
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            out[rel] = "dir"
    return out


def make_home(path: Path) -> Path:
    path.mkdir(parents=True)
    for name, text in SENTINELS.items():
        (path / name).write_text(text)
    return path


def run_case(
    sandbox_cli: str,
    binary: Path,
    controller: Path,
    job: Path,
    home: Path,
    runtime: Path | str | None,
    extra: list[str] | None = None,
) -> dict:
    before = digest_tree(home)
    audit = WriteAudit(home)
    command = [
        sandbox_cli,
        "sandbox",
        "-c",
        'sandbox_mode="workspace-write"',
        "-c",
        "sandbox_workspace_write.network_access=false",
        "--",
        sys.executable,
        "-I",
        str(HERE / "e2e.py"),
        "--inner",
        str(binary),
        str(home),
        str(runtime) if runtime is not None else "-",
        json.dumps(extra or []),
    ]
    p = subprocess.Popen(
        command,
        cwd=job,
        env=clean_env(controller),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 60
    while p.poll() is None:
        audit.drain()
        if time.monotonic() > deadline:
            p.kill()
        time.sleep(0.05)
    out, err = p.communicate()
    events = audit.close()
    if p.returncode:
        msg = f"sandbox probe failed: exit={p.returncode}: {err[-2000:]}"
        raise RuntimeError(msg)
    result = json.loads(out.splitlines()[-1])
    result["home_write_events"] = sorted({f"{e}:{path}" for e, path in events})
    result["home_paths_written"] = sorted({path for _e, path in events})
    result["home_unchanged"] = before == digest_tree(home)
    result["sentinels_intact"] = all(
        (home / n).read_text() == t for n, t in SENTINELS.items()
    )
    return result


def normalized_paths(paths) -> set[str]:
    """Written paths without per-run random suffixes or bare parent directories.

    arg0 helper folders are `codex-arg0<random>`; whether inotify reports a
    directory's own create event depends on timing, so only leaves compare."""
    norm = {re.sub(r"codex-arg0[A-Za-z0-9]{6}", "codex-arg0*", p) for p in paths}
    return {p for p in norm if not any(q.startswith(p + "/") for q in norm)}


def list_tree(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(
        str(p.relative_to(root)) + ("/" if p.is_dir() else "") for p in root.rglob("*")
    )


def check(results: dict, name: str, cond: bool, detail: str = "") -> None:
    results.setdefault("checks", []).append(
        {"check": name, "passed": bool(cond), **({"detail": detail} if detail else {})}
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=False)
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument("--fixture-root", type=Path, required=False)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--inner", nargs=4)
    args = parser.parse_args()
    if args.inner:
        binary, home, runtime, extra = args.inner
        print(
            json.dumps(
                asyncio.run(inner(Path(binary), Path(home), runtime, json.loads(extra)))
            )
        )
        return 0

    sandbox_cli = shutil.which("codex")
    binary = args.binary.resolve()
    baseline = (args.baseline or Path(sandbox_cli)).resolve()
    args.fixture_root.mkdir(parents=True, exist_ok=True)
    results: dict = {
        "binary": str(binary),
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "baseline": str(baseline),
        "sandbox_cli_version": subprocess.run(
            [sandbox_cli, "--version"], capture_output=True, text=True, check=True
        ).stdout.strip(),
        "requests_sent": ["initialize", "initialized (only on success)"],
        "network_access": False,
        "cases": {},
    }
    cases = results["cases"]
    with tempfile.TemporaryDirectory(prefix="e2e-", dir=args.fixture_root) as tmp:
        fx = Path(tmp)
        controller = fx / "controller"
        controller.mkdir()
        job = fx / "job"
        job.mkdir()
        # Read-only by sandbox: outside the job's writable root.
        ro_home = make_home(fx / "readonly-home")
        state = job / ".ergo" / "codex-state"
        state.mkdir(parents=True)

        def fresh_root(n: str) -> Path:
            root = state / f"run-{n}"
            root.mkdir(mode=0o700)
            return root

        # 1. Baseline (unpatched) behaviour, writable synthetic home inside job.
        base_home = make_home(job / "baseline-writable-home")
        cases["baseline_writable_home"] = r = run_case(
            sandbox_cli, baseline, controller, job, base_home, None
        )
        check(results, "baseline initializes with writable home", r["initialized"])
        baseline_writes = set(r["home_paths_written"])
        check(
            results,
            "baseline writes runtime state into CODEX_HOME",
            "installation_id" in baseline_writes,
            ",".join(sorted(baseline_writes)),
        )

        # 2. Baseline with read-only home fails (the reported bug).
        cases["baseline_readonly_home"] = r = run_case(
            sandbox_cli, baseline, controller, job, ro_home, None
        )
        check(results, "baseline fails with read-only home", not r["initialized"])

        # 3. Patched, opt-in absent: identical default behaviour.
        def_home = make_home(job / "patched-default-home")
        cases["patched_default_writable_home"] = r = run_case(
            sandbox_cli, binary, controller, job, def_home, None
        )
        check(results, "patched default initializes", r["initialized"])
        check(
            results,
            "patched default writes the same CODEX_HOME paths as baseline",
            normalized_paths(r["home_paths_written"])
            == normalized_paths(baseline_writes),
            json.dumps(
                {
                    "only_baseline": sorted(
                        normalized_paths(baseline_writes)
                        - normalized_paths(r["home_paths_written"])
                    ),
                    "only_patched": sorted(
                        normalized_paths(r["home_paths_written"])
                        - normalized_paths(baseline_writes)
                    ),
                }
            ),
        )
        cases["patched_default_readonly_home"] = r = run_case(
            sandbox_cli, binary, controller, job, ro_home, None
        )
        check(
            results,
            "patched default still fails with read-only home (no silent behaviour change)",
            not r["initialized"],
        )

        # 4. Patched + runtime root, read-only home outside job: the fix.
        root1 = fresh_root("1")
        cases["patched_runtime_readonly_home"] = r = run_case(
            sandbox_cli, binary, controller, job, ro_home, root1
        )
        r["runtime_tree"] = list_tree(root1)
        check(
            results, "patched+runtime initializes with read-only home", r["initialized"]
        )
        check(results, "read-only home: zero write events", not r["home_write_events"])
        check(results, "read-only home: sentinels intact", r["sentinels_intact"])
        check(
            results,
            "synthetic auth/config writes denied inside sandbox",
            r["sentinel_writes_blocked"] == list(SENTINELS),
        )
        check(
            results,
            "installation_id created in runtime root",
            "installation_id" in r["runtime_tree"],
        )

        # 5. Patched + runtime root, *writable* home: proves no write attempts
        #    are merely being hidden by EROFS.
        audit_home = make_home(job / "audit-writable-home")
        root2 = fresh_root("2")
        cases["patched_runtime_writable_home_audit"] = r = run_case(
            sandbox_cli, binary, controller, job, audit_home, root2
        )
        r["runtime_tree"] = list_tree(root2)
        check(results, "writable-home audit initializes", r["initialized"])
        check(
            results,
            "writable-home audit: zero CODEX_HOME write events",
            not r["home_write_events"],
            ",".join(r["home_write_events"]),
        )
        moved = sorted(
            p
            for p in baseline_writes
            if p in {x.rstrip("/") for x in r["runtime_tree"]}
        )
        r["baseline_paths_now_in_runtime_root"] = moved

        # 6. Consecutive runs get distinct identities and no shared files.
        root3 = fresh_root("3")
        cases["patched_runtime_consecutive"] = r = run_case(
            sandbox_cli, binary, controller, job, ro_home, root3
        )
        ids = [
            (root / "installation_id").read_text().strip()
            for root in (root1, root3)
            if (root / "installation_id").exists()
        ]
        check(
            results,
            "consecutive runs: distinct installation ids",
            len(ids) == 2 and ids[0] != ids[1],
        )
        inodes = [
            {(p.stat().st_dev, p.stat().st_ino) for p in root.rglob("*") if p.is_file()}
            for root in (root1, root3)
        ]
        check(
            results, "consecutive runs: no shared inodes", not (inodes[0] & inodes[1])
        )

        # 7. Concurrent runs.
        from concurrent.futures import ThreadPoolExecutor

        roots = [fresh_root(f"c{i}") for i in range(3)]
        with ThreadPoolExecutor(3) as pool:
            concurrent = list(
                pool.map(
                    lambda rt: run_case(
                        sandbox_cli, binary, controller, job, ro_home, rt
                    ),
                    roots,
                )
            )
        cases["patched_runtime_concurrent"] = concurrent
        cids = {(rt / "installation_id").read_text().strip() for rt in roots}
        check(
            results,
            "concurrent runs: all initialize",
            all(c["initialized"] for c in concurrent),
        )
        check(results, "concurrent runs: distinct installation ids", len(cids) == 3)
        check(
            results,
            "concurrent runs: zero home write events",
            not any(c["home_write_events"] for c in concurrent),
        )

        # 8. Rejections: symlinked root, symlinked ancestor, overlap, relative,
        #    missing. Each must fail closed and never fall back to CODEX_HOME.
        target = fresh_root("symlink-target")
        link = state / "run-link"
        link.symlink_to(target)
        anc = job / "anc-link"
        anc.symlink_to(state)
        anc_target = fresh_root("anc")
        inside_home = job / "overlap-home"
        make_home(inside_home)
        (inside_home / "state").mkdir()
        rejects = {
            "symlink_root": (ro_home, link),
            "symlink_ancestor": (ro_home, anc / anc_target.name),
            "root_inside_codex_home": (inside_home, inside_home / "state"),
            "root_equals_codex_home": (inside_home, inside_home),
            "relative_root": (ro_home, "relative/state"),
            "missing_root": (ro_home, state / "does-not-exist"),
        }
        for name, (home, rt) in rejects.items():
            cases[f"reject_{name}"] = r = run_case(
                sandbox_cli, binary, controller, job, home, rt
            )
            check(
                results,
                f"reject {name}: fails closed",
                not r["initialized"] and r["runtime_home_error"],
                (r["stderr_tail"] or [""])[0][:300],
            )
            check(
                results,
                f"reject {name}: no CODEX_HOME writes",
                not r["home_write_events"],
                ",".join(r["home_write_events"]),
            )
        check(results, "reject symlink: target untouched", list_tree(target) == [])
        check(
            results,
            "reject missing: root not created",
            not (state / "does-not-exist").exists(),
        )

        # 9. Explicit state overrides outside runtime root are refused.
        root4 = fresh_root("4")
        outside = job / "outside-sqlite"
        outside.mkdir()
        cases["reject_sqlite_home_outside_root"] = r = run_case(
            sandbox_cli,
            binary,
            controller,
            job,
            ro_home,
            root4,
            ["-c", "sqlite_home=" + json.dumps(str(outside))],
        )
        check(
            results,
            "sqlite_home override outside runtime root refused",
            not r["initialized"] and list_tree(outside) == [],
            (r["stderr_tail"] or [""])[0][:300],
        )
        cases["reject_log_dir_outside_root"] = r = run_case(
            sandbox_cli,
            binary,
            controller,
            job,
            ro_home,
            fresh_root("5"),
            ["-c", "log_dir=" + json.dumps(str(outside))],
        )
        check(
            results,
            "log_dir override outside runtime root refused",
            not r["initialized"] and list_tree(outside) == [],
            (r["stderr_tail"] or [""])[0][:300],
        )
        check(
            results,
            "read-only home unchanged across all cases",
            all((ro_home / n).read_text() == t for n, t in SENTINELS.items()),
        )

    failed = [c for c in results["checks"] if not c["passed"]]
    results["summary"] = {
        "passed": len(results["checks"]) - len(failed),
        "failed": len(failed),
    }
    text = json.dumps(results, indent=2)
    if args.out:
        args.out.write_text(text + "\n")
    for c in results["checks"]:
        print(("PASS " if c["passed"] else "FAIL ") + c["check"])
        if not c["passed"] and c.get("detail"):
            print("     " + c["detail"])
    print(json.dumps(results["summary"]))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
