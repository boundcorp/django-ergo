#!/usr/bin/env python3
# ruff: noqa: S603, T201, PLR0915, PLR2004 -- standalone bounded regression probe
"""Offline source-level regression; never opens a user's auth/config home.

Copies only named PUBLIC SOURCE files, applies the proposed patch in a disposable
worktree-local tree, and compiles its production identity function unchanged.
The small Cargo harness aliases home-dir to the exact proposed runtime_home
module; this validates neither the full Codex workspace nor app-server linkage.
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = [
    "codex-rs/core/src/config/permissions.rs",
    "codex-rs/Cargo.lock",
    "codex-rs/utils/home-dir/src/lib.rs",
    "codex-rs/runtime-agent-instance/Cargo.toml",
    "codex-rs/runtime-agent-instance/src/installation_id.rs",
    "codex-rs/arg0/src/lib.rs",
]


def run(args, *, cwd, env=None):
    p = subprocess.run(
        args, cwd=cwd, env=env, text=True, capture_output=True, timeout=180, check=False
    )
    if p.returncode:
        msg = f"{args[0]} failed ({p.returncode}): {p.stderr[-4000:]}"
        raise RuntimeError(msg)
    return p.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    assert (
        run(["git", "rev-parse", "HEAD"], cwd=source)
        == "ccf9be8cc458bd6b8336fe84d68d8893f2691a5e"
    )
    with tempfile.TemporaryDirectory(prefix=".probe-", dir=HERE) as tmp_dir:
        tmp = Path(tmp_dir)
        upstream = tmp / "upstream"
        for name in FILES:
            target = upstream / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, target)
        run(
            ["git", "apply", "--check", str(HERE / "startup-runtime-home.patch")],
            cwd=upstream,
        )
        run(["git", "apply", str(HERE / "startup-runtime-home.patch")], cwd=upstream)
        runtime = upstream / "codex-rs/utils/home-dir/src/runtime_home.rs"
        assert runtime.read_bytes() == (HERE / "runtime_home.rs").read_bytes()
        env = os.environ.copy()
        env.pop("CODEX_RUNTIME_HOME", None)
        env["CODEX_FIXTURE_ROOT"] = str(tmp)
        run(
            [
                "rustc",
                "--edition=2024",
                "--test",
                str(runtime),
                "-o",
                str(tmp / "root-tests"),
            ],
            cwd=tmp,
        )
        suite = run([str(tmp / "root-tests")], cwd=tmp, env=env)
        assert "10 passed" in suite, suite
        crate = tmp / "harness"
        crate.mkdir()
        (crate / "Cargo.toml").write_text("""[package]
name = "codex-identity-source-probe"
version = "0.0.0"
edition = "2024"
[[bin]]
name = "identity-probe"
path = "main.rs"
[dependencies]
tokio = { version = "=1.47.1", features = ["fs", "rt", "macros"] }
uuid = { version = "=1.18.1", features = ["v4"] }
""")
        (crate / "main.rs").write_text("""extern crate self as codex_utils_home_dir;
#[path = "../upstream/codex-rs/utils/home-dir/src/runtime_home.rs"]
pub mod runtime_home;
#[path = "../upstream/codex-rs/runtime-agent-instance/src/installation_id.rs"]
mod installation_id;
#[tokio::main(flavor = "current_thread")]
async fn main() {
    let home = std::path::PathBuf::from(std::env::args_os().nth(1).expect("synthetic home"));
    match installation_id::resolve_installation_id(&home).await {
        Ok(id) => println!("{id}"),
        Err(error) => { eprintln!("{error}"); std::process::exit(1); }
    }
}
""")
        run(["cargo", "+stable", "build", "--offline", "--quiet"], cwd=crate, env=env)
        exe = crate / "target/debug/identity-probe"
        home = tmp / "synthetic-home"
        home.mkdir()
        for name, data in [
            ("auth.json", '{"OPENAI_API_KEY":null}\n'),
            ("config.toml", "# synthetic config\n"),
            ("installation_id", str(uuid.uuid4())),
        ]:
            (home / name).write_text(data)
        before = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in home.iterdir()
        }
        jobs = tmp / "jobs"
        jobs.mkdir()
        states = [jobs / f"run-{i}" for i in range(4)]
        for state in states:
            state.mkdir(mode=0o700)

        def probe(state):
            child_env = {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": str(home),
                "CODEX_RUNTIME_HOME": str(state),
            }
            return run([str(exe), str(home)], cwd=jobs, env=child_env)

        with concurrent.futures.ThreadPoolExecutor() as pool:
            ids = list(pool.map(probe, states))
        assert len(set(ids)) == 4
        with concurrent.futures.ThreadPoolExecutor() as pool:
            same_root_ids = list(pool.map(probe, [states[0]] * 8))
        assert set(same_root_ids) == {ids[0]}
        after = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in home.iterdir()
        }
        assert before == after
        # Kernel-enforced read-only home: installed CLI only supplies its sandbox;
        # the actual code under test is the proposed identity function above.
        controller = tmp / "sandbox-controller"
        controller.mkdir()
        outer_env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(controller),
            "CODEX_HOME": str(controller),
        }
        inner = jobs / "inside_sandbox.py"
        inner.write_text("""import errno, json, os, pathlib, subprocess, sys
home, state, exe = map(pathlib.Path, sys.argv[1:])
blocked = []
for name in ["auth.json", "config.toml", "installation_id"]:
    try:
        with (home/name).open("a") as f: f.write("SHOULD-NOT-WRITE")
    except OSError as error:
        assert error.errno in (errno.EROFS, errno.EACCES, errno.EPERM)
        blocked.append(name)
    else: raise AssertionError("synthetic home became writable")
env = os.environ.copy(); env["CODEX_RUNTIME_HOME"] = str(state)
p = subprocess.run([str(exe), str(home)], env=env, capture_output=True, text=True, timeout=20)
assert p.returncode == 0, p.stderr
print(json.dumps({"identity_succeeded": True, "synthetic_writes_blocked": blocked}))
""")
        sandbox = run(
            [
                "codex",
                "sandbox",
                "-c",
                'sandbox_mode="workspace-write"',
                "-c",
                "sandbox_workspace_write.network_access=false",
                "--",
                shutil.which("python3"),
                str(inner),
                str(home),
                str(states[0]),
                str(exe),
            ],
            cwd=jobs,
            env=outer_env,
        )
        assert before == {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in home.iterdir()
        }
        result = {
            "patch_applies": True,
            "resolver_tests_passed": 10,
            "identity_source_compiled": True,
            "separate_job_ids": 4,
            "concurrent_same_root_reuses_id": 8,
            "synthetic_home_unchanged": True,
            "kernel_sandbox": json.loads(sandbox.splitlines()[-1]),
            "full_codex_build": False,
            "patched_appserver_handshake": False,
        }
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
