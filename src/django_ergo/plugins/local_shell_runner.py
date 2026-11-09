"""Supervise one agent CLI run in a local_shell job folder.

    python3 local_shell_runner.py <job-folder> <run-number>

The local_shell plugin starts this detached (its own session), with the
agent's environment already scrubbed. It reads ``.ergo/run-<n>.json``
(``argv``, ``timeout_seconds``), runs the agent in the job folder in its own
process group with stdout to ``.ergo/run-<n>.jsonl`` and stderr to
``.ergo/run-<n>.err``, and while it runs touches ``.ergo/heartbeat`` so a
watcher in another process (or pod) can tell it's alive. A ``.ergo/stop``
file, or the timeout, stops the agent's process group. When it ends it writes
``.ergo/run-<n>.exit.json``: ``{"code", "reason", "ended_at"}``.

The agent and everything it starts get the highest OOM score, so when a job
(an ffmpeg pass, say) runs the container out of memory the kernel kills the
job, not the Ergonaut process that launched it; this supervisor survives and
records the kill (``reason: "killed"``).

Standard library only, so it runs wherever Python 3 does.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

BEAT_SECONDS = 5
KILL_GRACE_SECONDS = 10
OOM_SCORE_ADJ = Path("/proc/self/oom_score_adj")


def prefer_oom_kill() -> None:
    """Run in the agent's child before exec: be the first thing the OOM killer
    picks. Raising one's own score needs no privileges; it's best effort."""
    with contextlib.suppress(OSError):
        OOM_SCORE_ADJ.write_text("1000")


def write_json(path: Path, value: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value), encoding="utf-8")
    tmp.replace(path)


def stop_group(proc: subprocess.Popen) -> None:
    """TERM the agent's process group, then KILL it if it hasn't gone."""
    for sig, wait in ((signal.SIGTERM, KILL_GRACE_SECONDS), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            continue
        return


def main(folder: str, number: str) -> int:
    root = Path(folder)
    meta = root / ".ergo"
    spec = json.loads((meta / f"run-{number}.json").read_text(encoding="utf-8"))
    deadline = time.time() + float(spec.get("timeout_seconds") or 3600)
    beat = meta / "heartbeat"
    stop = meta / "stop"  # the plugin clears it before starting a run
    with (
        (meta / f"run-{number}.jsonl").open("ab") as out,
        (meta / f"run-{number}.err").open("ab") as err,
    ):
        try:
            proc = subprocess.Popen(  # noqa: S603 — argv from the plugin's own config
                spec["argv"],
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,
                preexec_fn=prefer_oom_kill,  # noqa: PLW1509 — writes one /proc file, no locks
            )
        except OSError as exc:
            err.write(f"Could not start agent: {exc}\n".encode())
            write_json(
                meta / f"run-{number}.exit.json",
                {"code": 1, "reason": "start_failed", "ended_at": time.time()},
            )
            return 1
        write_json(meta / f"run-{number}.pid.json", {"pid": proc.pid})
        reason = ""
        while proc.poll() is None:
            beat.touch()
            if stop.exists():
                reason = "stopped"
            elif time.time() > deadline:
                reason = "timeout"
            if reason:
                stop_group(proc)
                break
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=BEAT_SECONDS)
    code = proc.poll()
    if not reason and code == -signal.SIGKILL:
        reason = "killed"  # most likely the OOM killer (see prefer_oom_kill)
    write_json(
        meta / f"run-{number}.exit.json",
        {"code": code, "reason": reason or "exited", "ended_at": time.time()},
    )
    beat.touch()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
