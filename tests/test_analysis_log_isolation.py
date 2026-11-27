"""Blocker B7: user code can never write to the job pod's container log.

These run the real executor entrypoint in a subprocess whose stdout and stderr
pipes stand in for the container log stream. The PID-1 variants run it as init
of a fresh PID namespace (``unshare``), like the job container; CI runs the
same checks against the built runner image under runc and gVisor
(``.github/workflows/analysis-images.yml``).
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

WRAPPER = (
    "import pathlib, sys\n"
    "from django_ergo.analysis import executor\n"
    "executor.ROOT = pathlib.Path(sys.argv[1])\n"
    "executor.TERMINATION_LOG = pathlib.Path(sys.argv[2])\n"
    "executor.main()\n"
)

PROBE = Path(__file__).parents[1] / "infra" / "analysis" / "log_escape_probe.py"
ESCAPES = PROBE.read_text()
if Path("/dev/console").exists() and os.access("/dev/console", os.W_OK):
    ESCAPES = ESCAPES.replace("CHECK_CONSOLE = True", "CHECK_CONSOLE = False")

# Paths a script reaches only through its own bounded pipes.
OWN = {
    "/dev/stdout",
    "/dev/stderr",
    "/proc/self/fd/1",
    "/proc/self/fd/2",
    "/dev/fd/1",
}

needs_non_root = pytest.mark.skipif(
    os.geteuid() == 0, reason="root bypasses the non-dumpable /proc check"
)


def pid_namespace_available():
    if not shutil.which("unshare"):
        return False
    probe = subprocess.run(
        [
            "unshare",
            "--user",
            "--map-current-user",
            "--pid",
            "--fork",
            "--mount-proc",
            "true",
        ],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


needs_pid_namespace = pytest.mark.skipif(
    not pid_namespace_available(), reason="unprivileged PID namespaces unavailable"
)


def run_executor(tmp_path, request, *, pid1=False):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    termination = tmp_path / "termination-log"
    termination.write_bytes(b"")
    command = [sys.executable, "-I", "-c", WRAPPER, str(workspace), str(termination)]
    if pid1:
        command = [
            "unshare",
            "--user",
            "--map-current-user",
            "--pid",
            "--fork",
            "--mount-proc",
            *command,
        ]
    # A job container has no terminal: a new session has no controlling tty.
    proc = subprocess.run(
        command,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "ANALYSIS_REQUEST": json.dumps(request),
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        start_new_session=True,
        timeout=120,
        check=False,
    )
    return proc, termination.read_bytes()


def assert_only_record(proc, termination):
    """The container log is exactly one capped record, digested in the message."""
    assert proc.stderr == b""
    assert len(proc.stdout) <= 64 * 1024
    record = json.loads(proc.stdout)
    assert json.loads(termination) == {
        "v": 1,
        "bytes": len(proc.stdout),
        "sha256": hashlib.sha256(proc.stdout).hexdigest(),
    }
    return record


def escape_results(record):
    assert record["ok"] is True, record
    output = record["result"]["output"]
    line = next(x for x in output.splitlines() if x.startswith("RESULTS "))
    return json.loads(line.removeprefix("RESULTS ")), output


def check_escapes(results, output):
    assert results.pop("inherited") == []
    parent = results.pop("parent")
    # Non-dumpable: the parent's descriptors cannot even be listed, and its
    # memory and environment cannot be opened.
    assert results.pop("list parent fds") == "blocked:EACCES"
    assert results.pop(parent + "/mem") == "blocked:EACCES"
    assert results.pop(parent + "/environ") == "blocked:EACCES"
    assert results[parent + "/fd/1"] == "blocked:EACCES"
    assert sum(k.startswith(parent + "/fd/") for k in results) == 32
    for path, outcome in results.items():
        if path in OWN:
            # Lands in the bounded pipe, i.e. inside the record's output field.
            assert outcome == "wrote", path
            assert "ESCAPED " + path in output
        else:
            assert outcome != "wrote", (path, outcome)
            assert "ESCAPED " + path not in output
    assert results["/dev/tty"].startswith("blocked:")
    assert "INSIDE fd1" in output
    assert "INSIDE fd2" in output


@needs_non_root
def test_child_cannot_reach_container_log(tmp_path):
    proc, termination = run_executor(tmp_path, {"op": "python", "code": ESCAPES})
    record = assert_only_record(proc, termination)
    results, output = escape_results(record)
    # The executor, not pytest, is the script's parent.
    assert results["parent"] not in {f"/proc/{os.getpid()}", "/proc/1"}
    check_escapes(results, output)


@needs_non_root
@needs_pid_namespace
def test_child_cannot_reach_container_log_as_pid1(tmp_path):
    proc, termination = run_executor(
        tmp_path, {"op": "python", "code": ESCAPES}, pid1=True
    )
    record = assert_only_record(proc, termination)
    results, output = escape_results(record)
    # Inside the namespace the executor is PID 1 and the script's parent, so
    # this covers /proc/1/fd/1 (what /dev/stdout of PID 1 resolves to).
    assert results["parent"] == "/proc/1"
    check_escapes(results, output)


@needs_non_root
@pytest.mark.parametrize("fd", [1, 2])
def test_flooding_own_stdio_stays_inside_capped_record(tmp_path, fd):
    code = f"import os\nwhile True: os.write({fd}, b'x' * 65536)"
    proc, termination = run_executor(tmp_path, {"op": "python", "code": code})
    record = assert_only_record(proc, termination)
    assert record["result"]["reason"] == "output_limit"
    assert record["result"]["truncated"] is True


@needs_non_root
def test_termination_message_is_bounded_and_rewritten(tmp_path):
    path = tmp_path / "termination-log"
    code = (
        "import errno, os\n"
        f"fd = os.open({str(path)!r}, os.O_WRONLY)\n"
        "n = 0\n"
        "try:\n"
        "    while n < 96:\n"
        "        os.write(fd, b'y' * (1 << 20)); n += 1\n"
        "except OSError as exc:\n"
        "    print('stopped', n, errno.errorcode[exc.errno])\n"
    )
    proc, termination = run_executor(tmp_path, {"op": "python", "code": code})
    record = assert_only_record(proc, termination)
    assert record["result"]["output"] == "stopped 64 EFBIG\n"


@needs_non_root
@needs_pid_namespace
def test_escaped_daemon_is_killed_before_the_record(tmp_path):
    """A detached grandchild cannot outlive the script or touch the record."""
    path = tmp_path / "termination-log"
    marker = tmp_path / "workspace" / "late-marker"
    code = (
        "import os, time\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    if os.fork() == 0:\n"
        # Detach from the bounded pipe so the executor sees EOF and finishes.
        "        null = os.open(os.devnull, os.O_RDWR)\n"
        "        for n in (0, 1, 2): os.dup2(null, n)\n"
        f"        fd = os.open({str(path)!r}, os.O_WRONLY | os.O_APPEND)\n"
        "        end = time.monotonic() + 3\n"
        "        while time.monotonic() < end:\n"
        "            os.write(fd, b'junk')\n"
        "            time.sleep(0.001)\n"
        f"        open({str(marker)!r}, 'w').write('alive')\n"
        "    os._exit(0)\n"
        "time.sleep(0.2)\n"
        "print('parent done')\n"
    )
    proc, termination = run_executor(
        tmp_path, {"op": "python", "code": code}, pid1=True
    )
    record = assert_only_record(proc, termination)
    assert record["result"]["output"] == "parent done\n"
    time.sleep(3.5)
    assert not marker.exists()
    assert path.read_bytes() == termination


def test_python_job_refuses_without_log_isolation(tmp_path, monkeypatch):
    from django_ergo.analysis import executor

    monkeypatch.setattr(executor, "ROOT", tmp_path)
    monkeypatch.setattr(executor, "LOG_FD", None)
    with pytest.raises(executor.LogIsolationError):
        executor.run({"op": "python", "code": "print(1)"})
    # A dumpable process (pytest here) fails the probe: same-uid code could
    # open its descriptors through /proc.
    if os.geteuid() != 0:
        assert executor.log_stream_unreachable(1) is False
