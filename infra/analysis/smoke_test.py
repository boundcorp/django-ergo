#!/usr/bin/env python3
"""Smoke-test a built runner image the way a job pod runs it (blocker B7).

Usage: smoke_test.py RUNNER_IMAGE [--runtime runc|runsc]

Each case starts one container with the job pod's shape (uid 10001, read-only
root, no capabilities, no network, private /tmp, one workspace directory, the
four pod environment variables and a host-backed /dev/termination-log like the
kubelet's), then checks that the container log (`docker logs`, both streams)
is exactly the executor's one capped record and that the termination message
is that record's digest. Used by .github/workflows/analysis-images.yml under
runc and gVisor. Needs only the standard library and Docker.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

PROBE = Path(__file__).with_name("log_escape_probe.py").read_text()
OWN = {"/dev/stdout", "/dev/stderr", "/proc/self/fd/1", "/proc/self/fd/2", "/dev/fd/1"}

DAEMON = """
import os, time
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        null = os.open(os.devnull, os.O_RDWR)
        for n in (0, 1, 2):
            os.dup2(null, n)
        fd = os.open("/dev/termination-log", os.O_WRONLY | os.O_APPEND)
        end = time.monotonic() + 3
        while time.monotonic() < end:
            os.write(fd, b"junk")
            time.sleep(0.001)
        open("/workspace/late-marker", "w").write("alive")
    os._exit(0)
time.sleep(0.2)
print("parent done")
"""

TERMINATION_FILL = """
import errno, os
fd = os.open("/dev/termination-log", os.O_WRONLY)
n = 0
try:
    while n < 96:
        os.write(fd, b"y" * (1 << 20)); n += 1
except OSError as exc:
    print("stopped", n, errno.errorcode[exc.errno])
"""

IMPORTS = """
import duckdb, matplotlib, openpyxl, pandas, pyarrow
import matplotlib.pyplot as plt
plt.plot([1, 2]); plt.savefig("/workspace/plot.png")
print("imports ok")
"""


def docker(*args, check=True):
    return subprocess.run(["docker", *args], capture_output=True, check=check)


def run_case(image, runtime, code, linger=0):
    name = "analysis-smoke-" + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "workspace"
        workspace.mkdir()
        workspace.chmod(0o777)
        termination = Path(tmp) / "termination-log"
        termination.write_bytes(b"")
        termination.chmod(0o666)  # as the kubelet creates it
        request = json.dumps({"op": "python", "code": code})
        docker(
            "run", "-d", "--name", name, "--runtime", runtime,
            "--user", "10001:10001", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--network", "none",
            "--pids-limit", "64", "--memory", "512m", "--cpus", "1",
            "--tmpfs", "/tmp:rw,size=128m",
            "-v", f"{workspace}:/workspace",
            "-v", f"{termination}:/dev/termination-log",
            "-e", "ANALYSIS_REQUEST=" + request,
            "-e", "MPLBACKEND=Agg", "-e", "HOME=/tmp", "-e", "OPENBLAS_NUM_THREADS=1",
            image,
        )  # fmt: skip
        try:
            docker("wait", name)
            logs = docker("logs", name)
            time.sleep(linger)
            files = sorted(p.name for p in workspace.iterdir())
            return logs.stdout, logs.stderr, termination.read_bytes(), files
        finally:
            docker("rm", "-f", name, check=False)


def only_record(stdout, stderr, termination):
    assert stderr == b"", stderr[:300]
    assert len(stdout) <= 64 * 1024, len(stdout)
    record = json.loads(stdout)
    assert json.loads(termination) == {
        "v": 1,
        "bytes": len(stdout),
        "sha256": hashlib.sha256(stdout).hexdigest(),
    }, termination[:300]
    assert record["ok"] is True, record
    return record["result"]


def check_escapes(result):
    output = result["output"]
    line = next(x for x in output.splitlines() if x.startswith("RESULTS "))
    results = json.loads(line.removeprefix("RESULTS "))
    print(json.dumps(results, indent=1), file=sys.stderr)  # CI evidence, no secrets
    assert results.pop("inherited") == [], "inherited descriptors"
    parent = results.pop("parent")
    assert parent == "/proc/1", parent
    for key in ("list parent fds", parent + "/mem", parent + "/environ"):
        assert results.pop(key) == "blocked:EACCES", key
    for path, outcome in results.items():
        if path in OWN:
            assert outcome == "wrote" and "ESCAPED " + path in output, path
        else:
            assert outcome != "wrote" and "ESCAPED " + path not in output, path
    assert results["/proc/1/fd/1"] == "blocked:EACCES"
    assert results["/dev/tty"].startswith("blocked:")
    assert results["/dev/console"].startswith("blocked:")
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--runtime", default="runc")
    args = parser.parse_args()
    report = {}

    def case(name, code, linger=0):
        stdout, stderr, termination, files = run_case(
            args.image, args.runtime, code, linger
        )
        result = only_record(stdout, stderr, termination)
        report[name] = "ok"
        return result, files

    result, _ = case("escape-paths", PROBE)
    check_escapes(result)
    flood = "import os\nwhile True: os.write({}, b'x' * 65536)"
    for fd in (1, 2):
        result, _ = case(f"fd{fd}-flood", flood.format(fd))
        assert result["reason"] == "output_limit" and result["truncated"] is True
    result, _ = case("termination-fill", TERMINATION_FILL)
    assert result["output"] == "stopped 64 EFBIG\n", result["output"]
    # The daemon would write late-marker after 3s and append to the
    # termination file meanwhile; only_record() already proved the latter.
    result, files = case("escaped-daemon", DAEMON, linger=3.5)
    assert result["output"] == "parent done\n", result["output"]
    assert "late-marker" not in files, "daemon outlived the executor"
    result, files = case("imports", IMPORTS)
    assert result["output"] == "imports ok\n", result["output"]
    assert "plot.png" in files
    print(json.dumps({"runtime": args.runtime, "cases": report}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
