#!/usr/bin/env python3
# ruff: noqa: S603, T201, PT017 -- standalone bounded regression probe
"""Synthetic app-server initialize only, under a network-disabled outer sandbox.

No thread/start, turn/start, login, refresh, or model/media requests. The outer
and inner CODEX_HOME values are fixture directories; inherited credentials and
configuration variables are not passed to either CLI.
"""

import argparse
import asyncio
import contextlib
import errno
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent


def clean_env(home):
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "CODEX_HOME": str(home),
        "LANG": "C.UTF-8",
    }


async def inner(binary, home, state):
    blocked = []
    for name in ["auth.json", "config.toml"]:
        try:
            with (home / name).open("a") as stream:
                stream.write("SHOULD-NOT-WRITE")
        except OSError as error:
            assert error.errno in (errno.EROFS, errno.EACCES, errno.EPERM)
            blocked.append(name)
        else:
            # Writable control intentionally has a writable fixture home.
            with (home / name).open("rb+") as stream:
                stream.truncate(stream.seek(0, 2) - len("SHOULD-NOT-WRITE"))
    env = clean_env(home)
    env["CODEX_RUNTIME_HOME"] = str(state)
    p = await asyncio.create_subprocess_exec(
        str(binary),
        "-c",
        "sqlite_home=" + json.dumps(str(state / "sqlite")),
        "-c",
        "log_dir=" + json.dumps(str(state / "log")),
        "app-server",
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    stderr_task = asyncio.create_task(p.stderr.read())
    initialized = False
    try:
        p.stdin.write(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "clientInfo": {
                                "name": "synthetic-init-only",
                                "version": "0",
                            }
                        },
                    }
                )
                + "\n"
            ).encode()
        )
        await p.stdin.drain()

        async def response():
            while line := await p.stdout.readline():
                message = json.loads(line)
                if message.get("id") == 1:
                    return "result" in message
            return False

        initialized = await asyncio.wait_for(response(), timeout=25)
        if initialized:
            p.stdin.write(b'{"jsonrpc":"2.0","method":"initialized"}\n')
            await p.stdin.drain()
    finally:
        p.stdin.close()
        stdout_task = asyncio.create_task(p.stdout.read())
        try:
            await asyncio.wait_for(p.wait(), timeout=3)
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
        "read_only_error": "Read-only file system" in diagnostics,
        "synthetic_auth_config_writes_blocked": blocked,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=Path(shutil.which("codex")))
    parser.add_argument("--expect-patched", action="store_true")
    parser.add_argument("--inner", nargs=3)
    args = parser.parse_args()
    if args.inner:
        binary, home, state = map(Path, args.inner)
        print(json.dumps(asyncio.run(inner(binary, home, state))))
        return
    binary = args.binary.resolve()
    with tempfile.TemporaryDirectory(prefix=".handshake-", dir=HERE) as fixture_dir:
        fixture = Path(fixture_dir)
        controller = fixture / "controller"
        job = fixture / "job"
        outside = fixture / "synthetic-readonly-home"
        writable = job / "synthetic-writable-home"
        for path in [controller, job, outside, writable]:
            path.mkdir(parents=True, exist_ok=True)
        for home in [outside, writable]:
            (home / "auth.json").write_text('{"OPENAI_API_KEY":null}\n')
            (home / "config.toml").write_text("# Synthetic fixture, no credentials.\n")
            (home / "installation_id").write_text(str(uuid.uuid4()))
        before = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in outside.iterdir()
        }
        results = {}
        for name, home in [
            ("read_only_home", outside),
            ("writable_unauthenticated_control", writable),
        ]:
            state = job / (name + "-state")
            state.mkdir(mode=0o700)
            # Installed codex only provides the outer sandbox. A --binary candidate
            # is used solely for the app-server child, never the shared runtime.
            command = [
                shutil.which("codex"),
                "sandbox",
                "-c",
                'sandbox_mode="workspace-write"',
                "-c",
                "sandbox_workspace_write.network_access=false",
                "--",
                sys.executable,
                str(HERE / "handshake.py"),
                "--inner",
                str(binary),
                str(home),
                str(state),
            ]
            p = subprocess.run(
                command,
                cwd=job,
                env=clean_env(controller),
                capture_output=True,
                text=True,
                timeout=40,
                check=False,
            )
            if p.returncode:
                msg = f"synthetic sandbox probe failed: exit={p.returncode}: {p.stderr[-2500:]}"
                raise RuntimeError(msg)
            results[name] = json.loads(p.stdout.splitlines()[-1])
        assert before == {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in outside.iterdir()
        }
        assert results["read_only_home"]["synthetic_auth_config_writes_blocked"] == [
            "auth.json",
            "config.toml",
        ]
        assert results["read_only_home"]["initialized"] == args.expect_patched, results
        assert results["writable_unauthenticated_control"]["initialized"], results
        results["synthetic_readonly_home_unchanged"] = True
        results["requests_sent"] = ["initialize", "initialized (only on success)"]
        results["network_access"] = False
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
