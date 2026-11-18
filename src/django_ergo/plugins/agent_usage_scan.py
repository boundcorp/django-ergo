"""Read coding-agent session files and return token usage as JSON.

This module intentionally uses only the standard library: the Orca plugin sends it
unchanged to the machine holding the worktree, where it runs as ``python3 -``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PARTS = ("input", "cache_write", "cache_read", "output", "reasoning", "requests")
EPOCH_MS = 10_000_000_000


def _at(value: Any) -> datetime | None:
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value / 1000 if value > EPOCH_MS else value, UTC)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (
            parsed.replace(tzinfo=UTC)
            if parsed.tzinfo is None
            else parsed.astimezone(UTC)
        )
    except ValueError:
        return None


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _rows(path: Path):
    try:
        with path.open(encoding="utf-8") as source:
            for line in source:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    yield row
    except OSError:
        return


def _empty() -> dict[str, int | str | None]:
    return {**dict.fromkeys(PARTS, 0), "first_at": None, "last_at": None}


def _add(
    models: dict[str, dict], model: str, usage: dict[str, int], at: datetime | None
) -> None:
    if not model or model == "<synthetic>":
        return
    result = models.setdefault(model, _empty())
    for part in PARTS:
        result[part] += max(0, int(usage.get(part, 0)))
    if at is not None:
        stamp = _iso(at)
        result["first_at"] = min(result["first_at"] or stamp, stamp)
        result["last_at"] = max(result["last_at"] or stamp, stamp)


def _inside(at: datetime | None, since: datetime, until: datetime) -> bool:
    return at is not None and since <= at <= until


def _claude(root: Path, cwd: str, since: datetime, until: datetime) -> dict[str, dict]:
    slug = "".join(char if char.isalnum() else "-" for char in cwd)
    paths = (root / ".claude" / "projects" / slug).glob("**/*.jsonl")
    return _claude_models((_rows(path) for path in paths), since, until)


def _claude_models(sources, since: datetime, until: datetime) -> dict[str, dict]:
    deduped: dict[tuple[str, str], tuple[str, dict[str, int], datetime]] = {}
    for source in sources:
        for row in source:
            message = row.get("message") or {}
            at = _at(row.get("timestamp"))
            if row.get("type") != "assistant" or not _inside(at, since, until):
                continue
            usage = message.get("usage") or {}
            message_id = str(message.get("id") or "")
            request_id = str(row.get("requestId") or message.get("requestId") or "")
            if not usage or not message_id or not request_id:
                continue
            counts = {
                "input": usage.get("input_tokens", 0),
                "cache_write": usage.get("cache_creation_input_tokens", 0),
                "cache_read": usage.get("cache_read_input_tokens", 0),
                "output": usage.get("output_tokens", 0),
                "requests": 1,
            }
            key = (message_id, request_id)
            candidate = (str(message.get("model") or ""), counts, at)
            existing = deduped.get(key)
            if existing is None or sum(counts.values()) > sum(existing[1].values()):
                deduped[key] = candidate
    models: dict[str, dict] = {}
    for model, usage, at in deduped.values():
        _add(models, model, usage, at)
    return models


def _codex(root: Path, cwd: str, since: datetime, until: datetime) -> dict[str, dict]:
    models: dict[str, dict] = {}
    for path in (root / ".codex" / "sessions").glob("*/*/*/rollout-*.jsonl"):
        rows = list(_rows(path))
        meta = next((row for row in rows if row.get("type") == "session_meta"), {})
        if (meta.get("payload") or {}).get("cwd") != cwd:
            continue
        _codex_file(models, rows, since, until)
    return models


def _codex_file(
    models: dict[str, dict], rows: list[dict], since: datetime, until: datetime
) -> None:
    model = ""
    previous_total: dict[str, int] | None = None
    previous_snapshot: tuple[int, int, int, int] | None = None
    for row in rows:
        payload = row.get("payload") or {}
        if row.get("type") == "turn_context":
            model = str(payload.get("model") or model)
            continue
        if row.get("type") != "event_msg" or payload.get("type") != "token_count":
            continue
        at = _at(row.get("timestamp"))
        info = payload.get("info")
        if not isinstance(info, dict):
            continue
        usage = info.get("last_token_usage")
        if not isinstance(usage, dict):
            total = info.get("total_token_usage")
            if not isinstance(total, dict):
                continue
            current_total = {key: int(value or 0) for key, value in total.items()}
            usage = {
                key: value - (previous_total or {}).get(key, 0)
                for key, value in current_total.items()
            }
            previous_total = current_total
        if not _inside(at, since, until):
            continue
        cached = int(usage.get("cached_input_tokens", 0) or 0)
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output = int(usage.get("output_tokens", 0) or 0)
        reasoning = int(usage.get("reasoning_output_tokens", 0) or 0)
        snapshot = (input_tokens, cached, output, reasoning)
        if snapshot == previous_snapshot:
            continue
        previous_snapshot = snapshot
        _add(
            models,
            model,
            {
                "input": max(0, input_tokens - cached),
                "cache_read": cached,
                "output": output,
                "reasoning": reasoning,
                "requests": 1,
            },
            at,
        )


# What local_shell calls in-process, where the job folders are.
claude_usage = _claude
codex_usage = _codex


def _omp(root: Path, cwd: str, since: datetime, until: datetime) -> dict[str, dict]:
    models: dict[str, dict] = {}
    for path in (root / ".omp" / "agent" / "sessions").glob("**/*.jsonl"):
        rows = list(_rows(path))
        # omp writes a mutable title record before the session header.  The
        # header remains authoritative wherever it appears in the file.
        header = next((row for row in rows if row.get("type") == "session"), {})
        if not header or header.get("cwd") != cwd:
            continue
        _omp_file(models, rows, since, until)
    return models


def _omp_file(
    models: dict[str, dict], rows: list[dict], since: datetime, until: datetime
) -> None:
    for row in rows:
        message = row.get("message") or {}
        at = _at(row.get("timestamp"))
        if (
            row.get("type") != "message"
            or message.get("role") != "assistant"
            or not _inside(at, since, until)
        ):
            continue
        usage = message.get("usage") or {}
        if not usage:
            continue
        _add(
            models,
            str(message.get("model") or ""),
            {
                "input": usage.get("input", 0),
                "cache_write": usage.get("cacheWrite", 0),
                "cache_read": usage.get("cacheRead", 0),
                "output": usage.get("output", 0),
                "requests": 1,
            },
            at,
        )
    return models


def canonical_remote(remote: str) -> str:
    """Normalize a git remote without preserving credentials or a .git suffix."""
    remote = remote.strip()
    if re.match(r"^[^/@:]+@[^/:]+:.+", remote):
        user_host, path = remote.split(":", 1)
        return f"ssh://{user_host.split('@', 1)[1].lower()}/{path.rstrip('/').removesuffix('.git')}"
    parsed = urlsplit(remote)
    if not parsed.hostname:
        return ""
    host = parsed.hostname.lower()
    if parsed.port:
        host = f"{host}:{parsed.port}"
    path = parsed.path.rstrip("/").removesuffix(".git")
    return f"{parsed.scheme.lower()}://{host}{path}"


def _env() -> dict[str, str] | None:
    """The scrubbed environment in-process; None (inherit) when this runs as a
    standalone script (``python3 -``), whose caller already scrubbed it."""
    if __name__ == "__main__":
        return None
    from django_ergo.protected_env import scrubbed_env

    return scrubbed_env()


def _git(cwd: str, *args: str) -> str:
    try:
        return subprocess.run(  # noqa: S603 — fixed executable, cwd is an argument.
            ["git", "-C", cwd, *args],  # noqa: S607 — resolved from trusted PATH.
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
            env=_env(),
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def project(cwd: str, host_namespace: str = "local") -> dict[str, str]:
    """The repository a worktree belongs to: every clone and worktree of one remote
    shares ``key``; a repository without a remote gets a key local to this host."""
    root = _git(cwd, "rev-parse", "--show-toplevel")
    if not root:
        local = str(Path(cwd).resolve())
        return {
            "key": f"local:{_digest(f'{host_namespace}:{local}')}",
            "name": Path(local).name or local,
            "remote": "",
            "repo_root": local,
            "worktree": local,
            "branch": "",
            "confidence": "unresolved",
            "error": "not a git repository",
        }
    common = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    repo_root = str(Path(common or root).resolve())
    remotes = _git(cwd, "remote").splitlines()
    selected = (
        "origin" if "origin" in remotes else (sorted(remotes)[0] if remotes else "")
    )
    remote = (
        canonical_remote(_git(cwd, "remote", "get-url", selected)) if selected else ""
    )
    checkout = Path(repo_root)
    if checkout.name == ".git":
        checkout = checkout.parent
    found = {
        "remote": remote,
        "repo_root": repo_root,
        "worktree": root,
        "branch": _git(cwd, "rev-parse", "--abbrev-ref", "HEAD"),
        "error": "",
    }
    if remote:
        name = remote.rstrip("/").rsplit("/", 1)[-1]
        return {
            "key": f"remote:{_digest(remote)}",
            "name": name,
            "confidence": "remote",
            **found,
        }
    return {
        "key": f"local:{_digest(f'{host_namespace}:{repo_root}')}",
        "name": checkout.name,
        "confidence": "local",
        **found,
    }


def omp_history(root: Path, cwd: str) -> list[dict[str, str]]:
    """Return exact omp files for one cwd, including an optional title slot."""
    files = []
    for path in (root / ".omp" / "agent" / "sessions").glob("**/*.jsonl"):
        rows = list(_rows(path))
        header = next((row for row in rows if row.get("type") == "session"), {})
        if header.get("cwd") == cwd:
            files.append(
                {
                    "name": path.name,
                    "content": base64.b64encode(path.read_bytes()).decode("ascii"),
                }
            )
    return files


def scan(  # noqa: PLR0913
    agent: str,
    cwd: str,
    since: datetime,
    until: datetime,
    *,
    home: Path | None = None,
    include_history: bool = False,
    include_project: bool = False,
    project_only: bool = False,
) -> dict:
    """Return usage and, for supported formats, native bytes for one worktree; with
    ``include_project``, the repository it belongs to too (only that, ``project_only``)."""
    root = home or Path.home()
    if project_only:
        return {"project": project(cwd)}
    if agent == "claude":
        models = _claude(root, cwd, since, until)
    elif agent == "codex":
        models = _codex(root, cwd, since, until)
    elif agent == "omp":
        models = _omp(root, cwd, since, until)
    else:
        models = {}
    result = {"models": models}
    if include_history and agent == "omp":
        result["history"] = omp_history(root, cwd)
    if include_project:
        result["project"] = project(cwd)
    return result


def _row_dicts(data: bytes):
    for line in data.decode("utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            yield row


def interval_usage(agent: str, path: Path, end: int, sha256: str, inode: int) -> dict:
    """Usage of exactly bytes ``[0, end)`` of one session file, verified against the
    digest and inode Orca recorded when it closed the interval. No cwd or time scan."""
    stat = path.stat()
    if stat.st_ino != inode or stat.st_size < end:
        msg = "session file is not the one Orca recorded"
        raise ValueError(msg)
    with path.open("rb") as source:
        data = source.read(end)
    if (
        len(data) != end
        or hashlib.sha256(data).hexdigest() != sha256
        or (end and not data.endswith(b"\n"))
    ):
        msg = "session file bytes do not match the recorded interval"
        raise ValueError(msg)
    low, high = datetime.min.replace(tzinfo=UTC), datetime.max.replace(tzinfo=UTC)
    rows = list(_row_dicts(data))
    models: dict[str, dict] = {}
    if agent == "claude":
        models = _claude_models([rows], low, high)
    elif agent == "codex":
        _codex_file(models, rows, low, high)
    elif agent == "omp":
        _omp_file(models, rows, low, high)
    else:
        msg = f"unsupported agent {agent}"
        raise ValueError(msg)
    return {"models": models}


def interval_main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("agent", choices=("claude", "codex", "omp"))
    parser.add_argument("path")
    parser.add_argument("end", type=int)
    parser.add_argument("sha256")
    parser.add_argument("inode", type=int)
    args = parser.parse_args(argv)
    try:
        result = interval_usage(
            args.agent, Path(args.path), args.end, args.sha256, args.inode
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}))  # noqa: T201
        raise SystemExit(1) from exc
    print(json.dumps(result))  # noqa: T201


def main() -> None:
    if sys.argv[1:2] == ["interval"]:
        interval_main(sys.argv[2:])
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("agent", choices=("claude", "codex", "omp"))
    parser.add_argument("cwd")
    parser.add_argument("since")
    parser.add_argument("until")
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--project", action="store_true")
    parser.add_argument("--project-only", action="store_true")
    args = parser.parse_args()
    print(  # noqa: T201
        json.dumps(
            scan(
                args.agent,
                args.cwd,
                _at(args.since),
                _at(args.until),
                include_history=args.history,
                include_project=args.project,
                project_only=args.project_only,
            ),
        )
    )


if __name__ == "__main__":
    main()
