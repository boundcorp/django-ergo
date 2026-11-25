"""Metadata-only consumer for Orca's exact nativeUsageIdentity export.

This stdlib-only module runs on the attested files host. It never searches by cwd
or time, copies transcripts, or grants access. The caller must authorize the
worker's owner/session and resolve expected_host from trusted host configuration.
No ledger ownership is assigned here: the upstream profile is still unverified.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC
from datetime import datetime
from pathlib import Path

MAX_BYTES = 64 * 1024 * 1024


def _dict(value):
    return value if isinstance(value, dict) else {}


def _text(value):
    return value if isinstance(value, str) and value.strip() else ""


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (
            parsed.astimezone(UTC).isoformat()
            if parsed.utcoffset() is not None
            else None
        )
    except ValueError:
        return None


def _unknown(reason):
    return {"status": "unavailable", "reason": reason, "events": None}


def _events(rows, agent, session_id):  # noqa: C901, PLR0912
    events = {}
    model = ""
    previous_total = None
    for row in rows:
        stamp = _timestamp(row.get("timestamp"))
        payload = _dict(row.get("payload"))
        if agent == "codex" and row.get("type") == "turn_context":
            model = _text(payload.get("model"))
        if agent == "claude":
            if row.get("type") != "assistant":
                continue
            if row.get("sessionId") != session_id:
                return None
            message = _dict(row.get("message"))
            usage = _dict(message.get("usage"))
            request = _text(row.get("requestId"))
            message_id = _text(message.get("id"))
            model = _text(message.get("model"))
            if not request or not message_id:
                continue
            key = json.dumps([message_id, request], separators=(",", ":"))
            counts = {
                "input_tokens": _count(usage.get("input_tokens")),
                "output_tokens": _count(usage.get("output_tokens")),
                "cache_read_tokens": _count(usage.get("cache_read_input_tokens")),
                "cache_write_tokens": _count(usage.get("cache_creation_input_tokens")),
                "reasoning_tokens": None,
            }
        else:
            if row.get("type") != "event_msg" or payload.get("type") != "token_count":
                continue
            info = _dict(payload.get("info"))
            total = _dict(info.get("total_token_usage"))
            usage = _dict(info.get("last_token_usage"))
            if not usage and total:
                usage = {}
                for name, value in total.items():
                    current = _count(value)
                    before = (previous_total or {}).get(name, 0)
                    usage[name] = (
                        current - before
                        if current is not None
                        and before is not None
                        and current >= before
                        else None
                    )
            if total:
                previous_total = {name: _count(value) for name, value in total.items()}
            if not usage:
                continue
            cached = _count(usage.get("cached_input_tokens"))
            inputs = _count(usage.get("input_tokens"))
            counts = {
                "input_tokens": inputs - cached
                if inputs is not None and cached is not None and inputs >= cached
                else None,
                "cache_read_tokens": cached,
                "cache_write_tokens": None,
                "output_tokens": _count(usage.get("output_tokens")),
                "reasoning_tokens": _count(usage.get("reasoning_output_tokens")),
            }
            if all(value in (0, None) for value in counts.values()):
                continue
            # Timestamp is part of identity: equal counts at another time are
            # distinct requests, repeated notifications at the same time are not.
            key = json.dumps([stamp, counts], sort_keys=True, separators=(",", ":"))
        if not stamp or not model or model == "<synthetic>":
            continue
        existing = events.get(key)
        if existing:
            if existing["model"] != model or existing["occurred_at"] != stamp:
                return None
            counts = {
                name: max(value, existing[name])
                if value is not None and existing[name] is not None
                else value
                if value is not None
                else existing[name]
                for name, value in counts.items()
            }
        events[key] = {
            "source_event_key": hashlib.sha256(key.encode()).hexdigest(),
            "occurred_at": stamp,
            "model": model,
            **counts,
        }
    return list(events.values())


def collect(shown, *, dispatch_id: str, expected_host: str, home: Path) -> dict:  # noqa: C901, PLR0911, PLR0912
    """Consume a trusted worker-show response on its authorized host, fail closed.

    Return token metadata only. An observed session alone is insufficient: require
    the upstream fresh/exclusive byte interval, file identity, digest and row IDs.
    Missing receipts, profiles and timestamps are unknown, never zero coverage.
    """
    shown = _dict(shown)
    dispatch = _dict(shown.get("dispatch"))
    identity = _dict(shown.get("nativeUsageIdentity"))
    if not dispatch_id or dispatch.get("id") != dispatch_id:
        return _unknown("dispatch_mismatch")
    if identity.get("status") != "observed":
        return _unknown("session_not_observed")
    if not expected_host or identity.get("hostScope") != expected_host:
        return _unknown("host_scope_mismatch")
    for name in ("paneKey", "processIncarnation", "launchTokenHash", "sessionId"):
        if not _text(identity.get(name)):
            return _unknown("identity_incomplete")
    observed = identity.get("observedAt")
    if type(observed) not in (int, float) or not math.isfinite(observed):
        return _unknown("identity_incomplete")
    agent, session_id = identity.get("agent"), identity["sessionId"]
    if agent not in ("claude", "codex"):
        return _unknown("agent_unsupported")
    interval = _dict(identity.get("usageInterval"))
    end = interval.get("endOffset")
    if (
        interval.get("status") != "closed"
        or interval.get("basis") != "fresh_session_exclusive_interval"
        or type(interval.get("startOffset")) is not int
        or interval["startOffset"] != 0
        or type(end) is not int
        or not 0 < end <= MAX_BYTES
    ):
        return _unknown("exclusive_interval_missing")
    root = home / (".claude/projects" if agent == "claude" else ".codex/sessions")
    try:
        path = Path(_text(interval.get("transcriptPath")))
        if not path.is_absolute() or not path.resolve().is_relative_to(root.resolve()):
            return _unknown("path_outside_agent_home")
        with path.open("rb") as source:
            import os

            stat = os.fstat(source.fileno())
            file = _dict(interval.get("file"))
            if file.get("dev") != stat.st_dev or file.get("ino") != stat.st_ino:
                return _unknown("file_replaced")
            raw = source.read(end)
        if len(raw) != end or not raw.endswith(b"\n"):
            return _unknown("interval_incomplete")
        if hashlib.sha256(raw).hexdigest() != interval.get("sha256"):
            return _unknown("digest_mismatch")
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    except (OSError, ValueError, UnicodeError):
        return _unknown("interval_unreadable")
    if not all(isinstance(row, dict) for row in rows):
        return _unknown("invalid_records")
    if agent == "codex":
        ids = [
            _dict(row.get("payload")).get("id")
            for row in rows
            if row.get("type") == "session_meta"
        ]
        if ids != [session_id]:
            return _unknown("session_mismatch")
    events = _events(rows, agent, session_id)
    if events is None:
        return _unknown("session_or_event_conflict")
    if not events:
        return _unknown("timestamped_usage_missing")
    return {
        "status": "collected",
        "agent": agent,
        "session_id": session_id,
        "events": events,
        "profile": {"status": "unavailable", "reason": "profile_not_verified"},
        "coverage": "partial_or_unknown; missing timestamped usage is not zero",
        "ownership": "closed exclusive interval asserted by trusted Orca; ledger attachment requires authorized owner and verified profile",
    }
