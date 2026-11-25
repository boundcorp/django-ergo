"""Synthetic metadata fixtures only; no native CLI/model execution."""

import copy
import hashlib
import json

import pytest

from django_ergo.plugins.orca_native_usage import collect

STAMP = "2026-11-25T22:00:00Z"


def receipt(tmp_path, agent, rows):
    root = tmp_path / (
        ".claude/projects/test" if agent == "claude" else ".codex/sessions"
    )
    root.mkdir(parents=True, exist_ok=True)
    path = root / "fixture-session.jsonl"
    raw = b"".join((json.dumps(row) + "\n").encode() for row in rows)
    path.write_bytes(raw)
    stat = path.stat()
    shown = {
        "dispatch": {"id": "dispatch-fixture"},
        "nativeUsageIdentity": {
            "status": "observed",
            "agent": agent,
            "sessionId": "session-fixture",
            "hostScope": "host-fixture",
            "paneKey": "pane-fixture",
            "processIncarnation": "process-fixture",
            "launchTokenHash": "hash-fixture",
            "observedAt": 1795644000000,
            "usageInterval": {
                "status": "closed",
                "basis": "fresh_session_exclusive_interval",
                "transcriptPath": str(path),
                "file": {"dev": stat.st_dev, "ino": stat.st_ino},
                "startOffset": 0,
                "endOffset": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            },
        },
    }
    return shown, path


def run(shown, tmp_path, **kwargs):
    return collect(
        shown,
        dispatch_id="dispatch-fixture",
        expected_host=kwargs.get("host", "host-fixture"),
        home=tmp_path,
    )


def claude_row(**kwargs):
    return {
        "type": "assistant",
        "sessionId": "session-fixture",
        "timestamp": STAMP,
        "requestId": "request-fixture",
        "message": {
            "id": "message-fixture",
            "model": "claude-fixture",
            "content": "DO NOT EXPORT THIS TRANSCRIPT TEXT",
            "usage": {"input_tokens": 12, "output_tokens": 4},
        },
        **kwargs,
    }


def test_claude_exact_session_dedup_and_no_transcript_export(tmp_path):
    larger = claude_row()
    larger["message"]["usage"]["output_tokens"] = 8
    shown, path = receipt(tmp_path, "claude", [claude_row(), larger, larger])
    # Bytes after the signed closed interval belong to nobody in this receipt.
    with path.open("ab") as source:
        source.write((json.dumps(claude_row(sessionId="other")) + "\n").encode())
    result = run(shown, tmp_path)
    assert result["status"] == "collected"
    assert len(result["events"]) == 1
    assert result["events"][0]["output_tokens"] == 8
    assert result["events"][0]["cache_read_tokens"] is None
    assert result["profile"]["status"] == "unavailable"
    assert "DO NOT EXPORT" not in json.dumps(result)
    # Same path and timestamp never substitute for exact native identity.
    shown["nativeUsageIdentity"]["sessionId"] = "another-session"
    assert run(shown, tmp_path)["events"] is None


def codex_row(stamp=STAMP, *, last=None, total=None):
    return {
        "type": "event_msg",
        "timestamp": stamp,
        "payload": {
            "type": "token_count",
            "info": {"last_token_usage": last, "total_token_usage": total},
        },
    }


def test_codex_exact_meta_and_timestamp_dedup_not_snapshot_only(tmp_path):
    usage = {"input_tokens": 20, "cached_input_tokens": 5, "output_tokens": 4}
    row = codex_row(last=usage)
    shown, _ = receipt(
        tmp_path,
        "codex",
        [
            {
                "type": "session_meta",
                "payload": {"id": "session-fixture", "cwd": "/same"},
            },
            {"type": "turn_context", "payload": {"model": "codex-fixture"}},
            row,
            row,
            codex_row("2026-11-25T22:00:01Z", last=usage),
            codex_row(None, last=usage),
        ],
    )
    events = run(shown, tmp_path)["events"]
    assert len(events) == 2
    assert sum(event["input_tokens"] for event in events) == 30
    assert events[0]["reasoning_tokens"] is None
    assert len({event["source_event_key"] for event in events}) == 2
    shown["nativeUsageIdentity"]["sessionId"] = "same-cwd-other-session"
    assert run(shown, tmp_path)["reason"] == "session_mismatch"


def test_codex_cumulative_deltas_and_unknown_malformed_counts(tmp_path):
    shown, _ = receipt(
        tmp_path,
        "codex",
        [
            {"type": "session_meta", "payload": {"id": "session-fixture"}},
            {"type": "turn_context", "payload": {"model": "codex-fixture"}},
            codex_row(
                total={"input_tokens": 20, "cached_input_tokens": 5, "output_tokens": 4}
            ),
            codex_row(
                "2026-11-25T22:00:01Z",
                total={
                    "input_tokens": 30,
                    "cached_input_tokens": 7,
                    "output_tokens": 6,
                },
            ),
            codex_row(
                "2026-11-25T22:00:02Z", last={"input_tokens": [], "output_tokens": True}
            ),
        ],
    )
    events = run(shown, tmp_path)["events"]
    assert len(events) == 2
    assert sum(event["input_tokens"] for event in events) == 23
    assert sum(event["output_tokens"] for event in events) == 6


@pytest.mark.parametrize("value", [None, [], True, "bad", 12, {}])
def test_missing_or_malformed_export_is_unknown(value, tmp_path):
    assert run(value, tmp_path)["events"] is None
    assert (
        run(
            {"dispatch": {"id": "dispatch-fixture"}, "nativeUsageIdentity": value},
            tmp_path,
        )["events"]
        is None
    )


def test_permissions_host_dispatch_and_interval_are_required(tmp_path):
    shown, _ = receipt(tmp_path, "claude", [claude_row()])
    assert run(shown, tmp_path, host="other")["reason"] == "host_scope_mismatch"
    wrong = copy.deepcopy(shown)
    wrong["dispatch"]["id"] = "another"
    assert run(wrong, tmp_path)["reason"] == "dispatch_mismatch"
    for value in (None, [], {}, {"status": "unavailable"}, {"status": "open"}):
        wrong = copy.deepcopy(shown)
        wrong["nativeUsageIdentity"]["usageInterval"] = value
        assert run(wrong, tmp_path)["reason"] == "exclusive_interval_missing"


def test_rewrite_replace_or_outside_home_is_unknown(tmp_path):
    shown, path = receipt(tmp_path, "claude", [claude_row()])
    path.write_bytes(path.read_bytes().replace(b"12", b"99"))
    assert run(shown, tmp_path)["reason"] == "digest_mismatch"
    shown["nativeUsageIdentity"]["usageInterval"]["file"]["ino"] += 1
    assert run(shown, tmp_path)["reason"] == "file_replaced"
    shown["nativeUsageIdentity"]["usageInterval"]["transcriptPath"] = (
        "/tmp/outside.jsonl"
    )
    assert run(shown, tmp_path)["reason"] == "path_outside_agent_home"


def test_missing_timestamp_is_unknown_not_zero_usage(tmp_path):
    shown, _ = receipt(tmp_path, "claude", [claude_row(timestamp=None)])
    assert run(shown, tmp_path) == {
        "status": "unavailable",
        "reason": "timestamped_usage_missing",
        "events": None,
    }
