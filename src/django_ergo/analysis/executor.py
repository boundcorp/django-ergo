# ruff: noqa: TRY003, EM101, PLR2004, C901
"""Credential-free job entrypoint ONLY. Never call from web, bot or Celery.

The container runtime is the security boundary. All parsing and arbitrary Python
happen here, in a pod mounting exactly one workspace and no credentials.

Container log isolation (blocker B7): the kubelet has no hard quota on container
logs, so nothing that runs user code may hold a writable handle on the container
stdout/stderr. ``main`` first moves the original stdout to one private
close-on-exec descriptor, points fds 0/1/2 at /dev/null, closes every other
inherited descriptor and marks this process non-dumpable, which makes
``/proc/<this pid>/fd`` unreadable to same-uid processes (and blocks ptrace and
pidfd_getfd). A probe child then confirms that the saved descriptor really is
unreachable before any user code starts; if the runtime does not enforce it,
the job fails closed. User Python gets only bounded pipes, a 64 MiB file-size
rlimit and no controlling terminal. Untrusted workspace files (CSV, Parquet,
XLSX) are parsed the same way, in a forked child that closed the log
descriptor first, so a parser bug gives no more than user Python has. PID 1
ignores SIGINT, the one signal a same-namespace process could otherwise use
to interrupt it. After the job the executor, as PID 1,
kills and reaps every remaining process before it writes the one capped result
record to the saved descriptor and a short digest of that record to the
termination-message file (which it truncates first, because the kubelet
bind-mounts that host file writable into the container).
"""

import base64
import hashlib
import json
import os
import re
import resource
import selectors
import signal
import stat
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path

from django_ergo.analysis.protocol import CHUNK_BYTES
from django_ergo.analysis.protocol import MAX_FILE_BYTES
from django_ergo.analysis.query import compile_select

ROOT = Path("/workspace")
RESULT_BYTES = 48 * 1024
# The only bytes this pod ever writes to its container log.
RECORD_BYTES = 64 * 1024
TERMINATION_LOG = Path("/dev/termination-log")
PR_SET_DUMPABLE = 4
FAILED = b'{"ok":false,"error":"Job failed; invalid input or resource limit"}'
UNISOLATED = b'{"ok":false,"error":"Container log isolation unavailable"}'
# Set by main() once isolation holds; python jobs refuse to run without it.
LOG_FD = None


class LogIsolationError(RuntimeError):
    """The container log stream might be reachable from user code."""


# Runs as the same uid as user code, as a child of the executor (PID 1 in the
# pod). Exit 0 only if opening the saved log descriptor via /proc is refused
# with a permission error (Linux and gVisor both give EACCES). Any other
# failure, such as a missing /proc or a wrong pid, proves nothing: fail closed.
PROBE = """
import errno, os, sys
try:
    os.close(os.open("/proc/%d/fd/%s" % (os.getppid(), sys.argv[1]), os.O_WRONLY))
except OSError as exc:
    sys.exit(0 if exc.errno in (errno.EACCES, errno.EPERM) else 4)
sys.exit(3)
"""
# Wall-clock budget for one job step, inside the pod's 60s activeDeadline.
JOB_SECONDS = 55


def filename(name):
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,180}", name)
        or ".." in name
    ):
        raise ValueError("Use a flat workspace filename without traversal")
    return name


def open_file(name, flags=os.O_RDONLY):
    fd = os.open(ROOT / filename(name), flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size > MAX_FILE_BYTES
    ):
        os.close(fd)
        raise ValueError("File must be bounded, regular and have exactly one link")
    return os.fdopen(fd, "rb" if flags == os.O_RDONLY else "r+b")


def digest(handle):
    value = hashlib.file_digest(handle, "sha256").hexdigest()
    handle.seek(0)
    return value


def read_frame(name):
    import pandas as pd

    with open_file(name) as handle:
        version = digest(handle)
        suffix = Path(name).suffix.lower()
        if suffix == ".csv":
            frame = pd.read_csv(handle)
        elif suffix == ".parquet":
            frame = pd.read_parquet(handle)
        elif suffix == ".xlsx":
            frame = pd.read_excel(handle, engine="openpyxl")
        else:
            raise ValueError("Supported formats: CSV, Parquet, XLSX")
    names = list(map(str, frame.columns))
    if (
        len(names) > 128
        or len({c.casefold() for c in names}) != len(names)
        or any(not c or len(c.encode()) > 256 for c in names)
    ):
        raise ValueError("Dataset has too many, ambiguous or oversized columns")
    frame.columns = names
    return frame, version


def dataset(request):
    frame, version = read_frame(request["file"])
    if request.get("version") and request["version"] != version:
        raise ValueError("Dataset version changed; reopen it")
    if request["op"] == "dataset":
        # A separate immutable-by-version snapshot: later scripts cannot silently alter cursors.
        name = "dataset-" + str(uuid.uuid4()) + ".parquet"
        with open_file(name, os.O_RDWR | os.O_CREAT | os.O_EXCL) as handle:
            frame.to_parquet(handle, index=False)
            if os.fstat(handle.fileno()).st_size > MAX_FILE_BYTES:
                raise ValueError("Dataset snapshot exceeds file cap")
            handle.seek(0)
            version = digest(handle)
        return {
            "file": name,
            "version": version,
            "rows": len(frame),
            "schema": [{"name": c, "type": str(t)} for c, t in frame.dtypes.items()],
        }
    import duckdb

    spec = request.get("query", {})
    compiled = compile_select(spec, list(frame.columns))
    offset = request.get("offset", 0)
    if type(offset) is not int or not 0 <= offset <= 10**9:
        raise ValueError("Invalid cursor offset")
    with duckdb.connect(
        config={
            "enable_external_access": False,
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
            "allow_unsigned_extensions": False,
            "allow_community_extensions": False,
            "threads": 1,
            "memory_limit": "256MB",
        }
    ) as con:
        con.register("data", frame)
        con.execute("SET lock_configuration = true")
        total = con.execute(
            "SELECT count(*) FROM (" + compiled.sql + ") q",  # noqa: S608 — validated compiler output only
            compiled.params,
        ).fetchone()[0]
        result = con.execute(
            compiled.sql + " LIMIT ? OFFSET ?",
            compiled.params + (spec.get("limit", 100) + 1, offset),
        )
        rows = []
        more = False
        for row in result.fetchall():
            if len(rows) == spec.get("limit", 100):
                more = True
                break
            converted = [
                cell
                if cell is None or isinstance(cell, str | int | bool)
                else str(cell)
                for cell in row
            ]
            # Floats serialize as strings to preserve finite/nonfinite values without invalid JSON.
            if (
                len(json.dumps(rows + [converted], ensure_ascii=True).encode())
                > RESULT_BYTES - 4096
            ):
                if not rows:
                    raise ValueError(
                        "One row exceeds the byte cap; project smaller columns"
                    )
                more = True
                break
            rows.append(converted)
    return {
        "columns": compiled.columns,
        "rows": rows,
        "total_rows": total,
        "count_kind": "exact",
        "source_rows": len(frame),
        "version": version,
        "next_offset": offset + len(rows) if more else None,
        "truncated": more,
        "provenance": "Full dataset; WHERE precedes grouping; aggregates never use a sample",
    }


def child_limits():
    # Bounds every regular file user code writes, including the kubelet's
    # host-backed termination-message file; workspace files share this cap.
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES, MAX_FILE_BYTES))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    signal.signal(signal.SIGINT, signal.SIG_DFL)


def isolate_log_stream():
    """Move the container stdout out of reach.

    Returns its private descriptor and whether this process is now non-dumpable.
    """
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    os.closerange(3, min(soft, 65536))
    log = os.dup(1)  # os.dup is non-inheritable (close-on-exec) by default
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd, inheritable=False)
    os.close(devnull)
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    return log, libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) == 0


def log_stream_unreachable(log_fd):
    """Prove, as the user's uid, that the saved descriptor cannot be opened."""
    try:
        probe = subprocess.run(  # noqa: S603 — fixed probe, no user input
            [sys.executable, "-I", "-S", "-c", PROBE, str(log_fd)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={},
            start_new_session=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def reap_everything():
    """As PID 1, kill and reap every other process in the pod's namespace."""
    if os.getpid() != 1:
        return
    while True:
        with suppress(ProcessLookupError):
            os.kill(-1, signal.SIGKILL)
        try:
            os.waitpid(-1, 0)
        except ChildProcessError:
            return


def python_job(code):
    if not isinstance(code, str) or len(code.encode()) > 32 * 1024:
        raise ValueError("Code exceeds byte cap")
    if LOG_FD is None or not log_stream_unreachable(LOG_FD):
        raise LogIsolationError
    proc = subprocess.Popen(  # noqa: S603 — only inside credential-free job container
        [sys.executable, "-I", "-"],
        cwd=ROOT,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/tmp",  # noqa: S108 — pod-private bounded tmpfs
            "MPLBACKEND": "Agg",
            "OPENBLAS_NUM_THREADS": "1",
        },
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        # No controlling terminal (/dev/tty fails) and only fds 0/1/2.
        start_new_session=True,
        close_fds=True,
        preexec_fn=child_limits,  # noqa: PLW1509 — single-threaded executor
    )
    output = bytearray()
    deadline = time.monotonic() + JOB_SECONDS
    reason = "exited"
    try:
        # Python reads the whole script before executing: source stays out of argv.
        proc.stdin.write(code.encode())
        proc.stdin.close()
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    reason = "timeout"
                    break
                for key, _ in selector.select(timeout=0.1):
                    chunk = os.read(key.fd, 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk[: RESULT_BYTES - len(output)])
                    if len(output) >= RESULT_BYTES:
                        reason = "output_limit"
                        break
                if reason != "exited":
                    break
    finally:
        # Kill descendants even when the parent exited; pod deletion reaps escaped groups.
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        proc.stdout.close()
        proc.stdin.close()
        reap_everything()
    return {
        "output": output.decode(errors="replace"),
        "exit_code": proc.returncode,
        "reason": reason,
        "truncated": reason == "output_limit",
    }


def parse_in_child(fn, request):
    """Run ``fn(request)`` (untrusted file parsing) in a forked child.

    The child closes the log descriptor before it touches any workspace file
    and stays non-dumpable (inherited from this process), so it cannot reopen
    the descriptor through /proc either. It returns its JSON result over a
    bounded pipe; its error messages may contain file data and are dropped.
    """
    if LOG_FD is None or not log_stream_unreachable(LOG_FD):
        raise LogIsolationError
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:
        status = 1
        try:
            os.close(read_end)
            os.close(LOG_FD)
            os.setsid()
            child_limits()
            raw = json.dumps(fn(request), allow_nan=False).encode()
            write_all(write_end, raw[: RECORD_BYTES + 1])
            status = 0
        except BaseException:  # noqa: BLE001, S110 — never report parser errors
            pass
        finally:
            os._exit(status)
    os.close(write_end)
    data = bytearray()
    deadline = time.monotonic() + JOB_SECONDS
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(read_end, selectors.EVENT_READ)
            while len(data) <= RECORD_BYTES and time.monotonic() < deadline:
                if not selector.select(timeout=0.1):
                    continue
                chunk = os.read(read_end, 65536)
                if not chunk:
                    break
                data.extend(chunk)
    finally:
        os.close(read_end)
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        _, status = os.waitpid(pid, 0)
        reap_everything()
    if (
        not os.WIFEXITED(status)
        or os.WEXITSTATUS(status) != 0
        or len(data) > RECORD_BYTES
    ):
        raise ValueError("Parser failed or exceeded its limits")
    return json.loads(data)


def run(request):
    op = request["op"]
    if op == "python":
        return python_job(request["code"])
    if op in {"dataset", "select"}:
        return parse_in_child(dataset, request)
    if op == "list":
        entries = []
        for entry in ROOT.iterdir():
            if len(entries) >= 100:
                return {"files": entries, "truncated": True}
            try:
                with open_file(entry.name) as handle:
                    entries.append(
                        {"file": entry.name, "bytes": os.fstat(handle.fileno()).st_size}
                    )
            except (OSError, ValueError):
                continue
        return {"files": entries, "truncated": False}
    if op == "write":
        chunk = base64.b64decode(request["data"], validate=True)
        offset = request["offset"]
        if (
            len(chunk) > CHUNK_BYTES
            or type(offset) is not int
            or not 0 <= offset <= MAX_FILE_BYTES - len(chunk)
        ):
            raise ValueError("Transfer cap exceeded")
        flags = os.O_RDWR | (os.O_CREAT | os.O_EXCL if offset == 0 else 0)
        with open_file(request["file"], flags) as handle:
            if os.fstat(handle.fileno()).st_size != offset:
                raise ValueError("Transfer offset mismatch")
            handle.seek(offset)
            handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        return {"bytes": offset + len(chunk)}
    if op == "read":
        offset = request["offset"]
        if type(offset) is not int or not 0 <= offset <= MAX_FILE_BYTES:
            raise ValueError("Invalid transfer offset")
        with open_file(request["file"]) as handle:
            version = digest(handle)
            if request.get("version") and request["version"] != version:
                raise ValueError("File changed during export")
            handle.seek(offset)
            data = handle.read(CHUNK_BYTES)
            return {
                "data": base64.b64encode(data).decode(),
                "version": version,
                "size": os.fstat(handle.fileno()).st_size,
                "offset": offset,
            }
    raise ValueError("Unknown job operation")


def write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def write_termination_record(record):
    """Overwrite the termination message with a digest of the log record."""
    try:
        fd = os.open(
            TERMINATION_LOG, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC
        )
    except OSError:
        return
    try:
        write_all(
            fd,
            json.dumps(
                {
                    "v": 1,
                    "bytes": len(record),
                    "sha256": hashlib.sha256(record).hexdigest(),
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode(),
        )
    finally:
        os.close(fd)


def main():
    global LOG_FD  # noqa: PLW0603 — process-wide, set once at startup
    # Python's default SIGINT handler would let any same-namespace process
    # raise KeyboardInterrupt inside PID 1; children get SIG_DFL back.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    log, nondumpable = isolate_log_stream()
    LOG_FD = log if nondumpable else None
    try:
        request = json.loads(os.environ["ANALYSIS_REQUEST"])
        result = {"ok": True, "result": run(request)}
        raw = json.dumps(result, allow_nan=False).encode()
        if len(raw) > RECORD_BYTES:
            raise ValueError("Result exceeds output cap")  # noqa: TRY301
    except LogIsolationError:
        raw = UNISOLATED
    except (
        Exception  # noqa: BLE001 — untrusted parser messages must not escape
    ):  # The untrusted parser's exception may include file data; never log it.
        raw = FAILED
    reap_everything()
    write_all(log, raw[:RECORD_BYTES])
    write_termination_record(raw[:RECORD_BYTES])
    os.close(log)


if __name__ == "__main__":
    main()
