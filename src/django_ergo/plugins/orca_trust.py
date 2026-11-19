"""Atomically seed Claude Code folder trust for a verified Orca worktree.

This file is also executed over SSH with its source on stdin, so it uses only
the Python standard library and prints no credential or settings content.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path


def seed_trust(
    path: str, *, root: Path | None = None, home: Path | None = None
) -> None:
    root = (root or Path("/home/dev/orca/workspaces")).resolve()
    worktree = Path(path).resolve(strict=True)
    if root not in worktree.parents:
        msg = "Claude folder trust path is outside Orca workspaces"
        raise ValueError(msg)
    home = home or Path.home()
    target = home / ".claude.json"
    with (home / ".claude.json.orca-lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads(target.read_text()) if target.exists() else {}
        projects = data.setdefault("projects", {})
        projects.setdefault(str(worktree), {})["hasTrustDialogAccepted"] = True
        fd, temporary = tempfile.mkstemp(prefix=".claude.json.orca-", dir=home)
        try:
            os.fchmod(fd, target.stat().st_mode & 0o777 if target.exists() else 0o600)
            with os.fdopen(fd, "w") as output:
                json.dump(data, output)
                output.flush()
                os.fsync(output.fileno())
            Path(temporary).replace(target)
        finally:
            Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    seed_trust(sys.argv[1])
