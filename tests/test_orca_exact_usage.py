import hashlib
import json

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model

from django_ergo.bots.workers import WorkerContext
from django_ergo.conversation.models import AgentUsage
from django_ergo.conversation.models import Worker


def claude_row(message: str, tokens: int, at: str = "2026-11-18T10:00:00Z") -> str:
    row = {
        "type": "assistant",
        "timestamp": at,
        "requestId": f"req-{message}",
        "message": {
            "id": message,
            "model": "claude-sonnet",
            "usage": {"input_tokens": tokens, "output_tokens": 1},
        },
    }
    return json.dumps(row) + "\n"


def shown_for(
    dispatch: str, path, content: bytes, *, session_id: str, **overrides
) -> dict:
    interval = {
        "status": "closed",
        "basis": "fresh_session_exclusive_interval",
        "transcriptPath": str(path),
        "file": {"dev": 1, "ino": path.stat().st_ino, "birthtimeMs": 1.0},
        "startOffset": 0,
        "endOffset": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "closedAt": 1,
    }
    identity = {
        "status": "observed",
        "agent": "claude",
        "sessionId": session_id,
        "usageInterval": interval,
    }
    return {
        "dispatch": {"id": dispatch},
        "nativeUsageIdentity": {**identity, **overrides},
    }


@pytest.fixture
def harness(tmp_path, monkeypatch):
    from tests.test_bot_plugins import orca_bot

    bot, _, plugin = orca_bot(tmp_path, config="files_host: '', executable: orca-test")
    session = async_to_sync(bot.main_session)(
        get_user_model().objects.create(username="exact")
    )
    shown: dict[str, dict] = {}
    monkeypatch.setattr(plugin, "cli_json", lambda args: shown[args[-1]])

    def worker(dispatch: str) -> WorkerContext:
        made = Worker.objects.create(
            session=session,
            bot_name=bot.name,
            title="claude: task",
            function="orca:watch",
            state={"agent": "claude", "worktree": "/work/shared"},
        )
        return WorkerContext(bot, made)

    return plugin, worker, shown, tmp_path


def usage_by_worker(source: str) -> dict[int, int]:
    return {
        row.worker_id: row.input_tokens
        for row in AgentUsage.objects.filter(source=source)
    }


@pytest.mark.django_db
def test_fresh_interval_is_counted_once_and_rescans_are_idempotent(harness):
    plugin, worker, shown, tmp_path = harness
    ctx = worker("d1")
    sid = "11111111-aaaa"
    path = tmp_path / f"{sid}.jsonl"
    content = (claude_row("m1", 100) + claude_row("m2", 50)).encode()
    # Bytes after the closed interval (a later reader or resumed turn) are not ours.
    path.write_bytes(content + claude_row("m3", 7000).encode())
    shown["d1"] = shown_for("d1", path, content, session_id=sid)

    plugin.scan_exact_usage(ctx, "d1")
    plugin.scan_exact_usage(ctx, "d1")
    ctx.state.pop("exact_usage")
    plugin.scan_exact_usage(ctx, "d1")

    assert usage_by_worker("orca_exact") == {ctx.worker.pk: 150}
    [row] = AgentUsage.objects.all()
    assert (row.requests, row.output_tokens) == (2, 2)


@pytest.mark.django_db
def test_overlapping_workers_in_one_cwd_are_not_cross_counted(harness):
    plugin, worker, shown, tmp_path = harness
    first, second = worker("a"), worker("b")
    contents = {
        "a": ("a-session", (claude_row("a1", 10) + claude_row("a2", 20)).encode()),
        "b": ("b-session", claude_row("b1", 400).encode()),
    }
    for dispatch, (sid, content) in contents.items():
        path = tmp_path / f"{sid}.jsonl"
        path.write_bytes(content)
        shown[dispatch] = shown_for(dispatch, path, content, session_id=sid)

    plugin.scan_exact_usage(first, "a")
    plugin.scan_exact_usage(second, "b")

    assert usage_by_worker("orca_exact") == {first.worker.pk: 30, second.worker.pk: 400}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda shown: shown.pop("nativeUsageIdentity"), id="older-orca-no-field"
        ),
        pytest.param(
            lambda shown: shown["nativeUsageIdentity"].update(
                usageInterval={"status": "unavailable", "reason": "not_fresh_session"}
            ),
            id="resumed-session",
        ),
        pytest.param(
            lambda shown: shown["nativeUsageIdentity"].update(
                status="unavailable", reason="session_changed_or_shared"
            ),
            id="session-changed",
        ),
        pytest.param(
            lambda shown: shown["nativeUsageIdentity"]["usageInterval"].update(
                sha256="0" * 64
            ),
            id="bytes-do-not-match-digest",
        ),
        pytest.param(
            lambda shown: shown["nativeUsageIdentity"]["usageInterval"].update(
                startOffset=10
            ),
            id="start-not-attested",
        ),
        pytest.param(
            lambda shown: shown["nativeUsageIdentity"].update(agent="codex"),
            id="agent-mismatch",
        ),
    ],
)
def test_unattested_interval_records_nothing(harness, mutate):
    plugin, worker, shown, tmp_path = harness
    ctx = worker("d1")
    sid = "22222222-bbbb"
    path = tmp_path / f"{sid}.jsonl"
    content = claude_row("m1", 100).encode()
    path.write_bytes(content)
    shown["d1"] = shown_for("d1", path, content, session_id=sid)
    mutate(shown["d1"])

    plugin.scan_exact_usage(ctx, "d1")

    assert not AgentUsage.objects.exists()
