# ruff: noqa: TRY003, EM101, PLR2004, C901
"""Credential-free job entrypoint ONLY. Never call from web, bot or Celery.

The container runtime is the security boundary. All parsing and arbitrary Python
happen here, in a pod mounting exactly one workspace and no credentials.
"""

import base64
import hashlib
import json
import os
import re
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


def python_job(code):
    if not isinstance(code, str) or len(code.encode()) > 32 * 1024:
        raise ValueError("Code exceeds byte cap")
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
        start_new_session=True,
    )
    output = bytearray()
    deadline = time.monotonic() + 55
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
    return {
        "output": output.decode(errors="replace"),
        "exit_code": proc.returncode,
        "reason": reason,
        "truncated": reason == "output_limit",
    }


def run(request):
    op = request["op"]
    if op == "python":
        return python_job(request["code"])
    if op in {"dataset", "select"}:
        return dataset(request)
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


def main():
    try:
        request = json.loads(os.environ["ANALYSIS_REQUEST"])
        result = {"ok": True, "result": run(request)}
        raw = json.dumps(result, allow_nan=False).encode()
        if len(raw) > 64 * 1024:
            raise ValueError("Result exceeds output cap")  # noqa: TRY301
    except (
        Exception  # noqa: BLE001 — untrusted parser messages must not escape
    ):  # The untrusted parser's exception may include file data; never log it.
        raw = b'{"ok":false,"error":"Job failed; invalid input or resource limit"}'
    sys.stdout.buffer.write(raw)


if __name__ == "__main__":
    main()
