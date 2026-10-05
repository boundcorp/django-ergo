"""Read coding-agent session files and return token usage as JSON.

This module intentionally uses only the standard library: the Orca plugin sends it
unchanged to the machine holding the worktree, where it runs as ``python3 -``.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

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
    deduped: dict[tuple[str, str], tuple[str, dict[str, int], datetime]] = {}
    for path in (root / ".claude" / "projects" / slug).glob("**/*.jsonl"):
        for row in _rows(path):
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


def _codex(root: Path, cwd: str, since: datetime, until: datetime) -> dict[str, dict]:  # noqa: C901
    models: dict[str, dict] = {}
    for path in (root / ".codex" / "sessions").glob("*/*/*/rollout-*.jsonl"):
        rows = list(_rows(path))
        meta = next((row for row in rows if row.get("type") == "session_meta"), {})
        if (meta.get("payload") or {}).get("cwd") != cwd:
            continue
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
    return models


def _omp(root: Path, cwd: str, since: datetime, until: datetime) -> dict[str, dict]:
    models: dict[str, dict] = {}
    for path in (root / ".omp" / "agent" / "sessions").glob("*/*.jsonl"):
        rows = list(_rows(path))
        if not rows or rows[0].get("type") != "session" or rows[0].get("cwd") != cwd:
            continue
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


def scan(
    agent: str, cwd: str, since: datetime, until: datetime, *, home: Path | None = None
) -> dict[str, dict]:
    """Return usage for one agent/worktree in the inclusive UTC time window."""
    root = home or Path.home()
    if agent == "claude":
        models = _claude(root, cwd, since, until)
    elif agent == "codex":
        models = _codex(root, cwd, since, until)
    elif agent == "omp":
        models = _omp(root, cwd, since, until)
    else:
        models = {}
    return {"models": models}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("agent", choices=("claude", "codex", "omp"))
    parser.add_argument("cwd")
    parser.add_argument("since")
    parser.add_argument("until")
    args = parser.parse_args()
    print(  # noqa: T201
        json.dumps(
            scan(args.agent, args.cwd, _at(args.since), _at(args.until)),
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
