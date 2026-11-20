"""Report the provider/model omp actually used in one worktree since a launch.

omp's TUI header shows only a display name, and many providers share the same
display names ("Claude Sonnet 5.5" is offered by a dozen routers), so the header
cannot prove which provider answered. omp's session log can: every
``model_change`` entry names ``provider/model`` and every assistant message
records its ``provider`` and ``model``.

This file is also executed over SSH with its source on stdin, so it uses only
the Python standard library and prints model ids only, never message content.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _header_cwd(path: Path) -> str:
    with path.open("rb") as handle:
        for _ in range(5):
            line = handle.readline()
            if not line:
                break
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("type") == "session":
                return str(row.get("cwd") or "")
    return ""


def _row_model(row) -> tuple[str, bool]:
    """One session row's ``provider/model`` and fallback flag, if it has one."""
    if not isinstance(row, dict):
        return "", False
    if row.get("type") == "model_change":
        return str(row.get("model") or ""), bool(row.get("resolvedModelIsFallback"))
    message = row.get("message") or {}
    if row.get("type") == "message" and message.get("role") == "assistant":
        provider = str(message.get("provider") or "")
        model = str(message.get("model") or "")
        return (f"{provider}/{model}" if provider and model else ""), False
    return "", False


def _recent(sessions: Path, since: float) -> list[Path]:
    files = []
    if sessions.is_dir():
        for folder in os.scandir(sessions):
            if folder.is_dir():
                files += [
                    Path(entry.path)
                    for entry in os.scandir(folder.path)
                    if entry.name.endswith(".jsonl") and entry.stat().st_mtime >= since
                ]
    return sorted(files, key=lambda p: p.stat().st_mtime)


def session_models(cwd: str, since: float, *, root: Path | None = None) -> dict:
    """Distinct ``provider/model`` ids omp used in ``cwd`` sessions touched since
    ``since``, and whether omp flagged any of them as a fallback."""
    root = root or Path(
        os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".omp" / "agent"
    )
    models: list[str] = []
    fallback = False
    for path in _recent(root / "sessions", since):
        if _header_cwd(path) != cwd:
            continue
        with path.open("rb") as handle:
            for line in handle:
                try:
                    found, flagged = _row_model(json.loads(line))
                except ValueError:
                    continue
                fallback = fallback or flagged
                if found and found not in models:
                    models.append(found)
    return {"models": models, "fallback": fallback}


if __name__ == "__main__":
    print(json.dumps(session_models(sys.argv[1], float(sys.argv[2]))))  # noqa: T201
