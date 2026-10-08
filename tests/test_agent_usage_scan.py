import base64
import json
import shutil
from datetime import UTC
from datetime import datetime

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model

from django_ergo.conversation.models import AgentUsage
from django_ergo.conversation.models import Worker
from django_ergo.plugins.agent_usage_scan import scan

FIXTURES = __file__.replace("test_agent_usage_scan.py", "fixtures/agent_usage")
WINDOW = (
    datetime(2026, 10, 4, 11, 59, tzinfo=UTC),
    datetime(2026, 10, 4, 12, 5, tzinfo=UTC),
)


def test_scan_claude_dedupes_and_filters_by_time(tmp_path):
    path = tmp_path / ".claude" / "projects" / "-work-project" / "session"
    path.mkdir(parents=True)
    shutil.copy(f"{FIXTURES}/claude.jsonl", path / "session.jsonl")

    usage = scan("claude", "/work/project", *WINDOW, home=tmp_path)["models"]

    assert usage == {
        "claude-sonnet": {
            "input": 20,
            "cache_write": 4,
            "cache_read": 6,
            "output": 8,
            "reasoning": 0,
            "requests": 1,
            "first_at": "2026-10-04T12:00:01Z",
            "last_at": "2026-10-04T12:00:01Z",
        }
    }


def test_scan_codex_splits_cached_input_and_snapshot_deltas(tmp_path):
    path = tmp_path / ".codex" / "sessions" / "2026" / "10" / "04"
    path.mkdir(parents=True)
    shutil.copy(f"{FIXTURES}/codex.jsonl", path / "rollout-test.jsonl")

    usage = scan("codex", "/work/project", *WINDOW, home=tmp_path)["models"]

    assert usage["gpt-6-codex"] == {
        "input": 205,
        "cache_write": 0,
        "cache_read": 75,
        "output": 60,
        "reasoning": 5,
        "requests": 3,
        "first_at": "2026-10-04T12:00:00Z",
        "last_at": "2026-10-04T12:00:03Z",
    }


def test_scan_omp_matches_cwd_and_time_window(tmp_path):
    path = tmp_path / ".omp" / "agent" / "sessions" / "session"
    path.mkdir(parents=True)
    shutil.copy(f"{FIXTURES}/omp.jsonl", path / "worker.jsonl")

    usage = scan("omp", "/work/project", *WINDOW, home=tmp_path)["models"]

    assert usage["claude-opus"] == {
        "input": 10,
        "cache_write": 2,
        "cache_read": 3,
        "output": 4,
        "reasoning": 0,
        "requests": 1,
        "first_at": "2026-10-04T12:00:00Z",
        "last_at": "2026-10-04T12:00:00Z",
    }
    history = scan("omp", "/work/project", *WINDOW, home=tmp_path, include_history=True)
    assert base64.b64decode(history["history"][0]["content"]).startswith(
        b'{"type":"title"'
    )


@pytest.mark.django_db
def test_orca_usage_scan_stores_refreshes_and_survives_failure(tmp_path, monkeypatch):
    import subprocess

    from django_ergo.bots.workers import WorkerContext
    from tests.test_bot_plugins import orca_bot

    bot, _, plugin = orca_bot(
        tmp_path, config="environment: devhost, executable: orca-test"
    )
    user = get_user_model().objects.create(username="usage")
    session = async_to_sync(bot.main_session)(user)
    worker = Worker.objects.create(
        session=session,
        bot_name=bot.name,
        title="codex: Count tokens",
        function="orca:watch",
        state={"agent": "codex", "worktree": "/work/project"},
    )
    calls = []
    payload = {
        "models": {
            "gpt-6-codex": {
                "input": 70,
                "cache_write": 2,
                "cache_read": 30,
                "output": 25,
                "reasoning": 5,
                "requests": 1,
                "first_at": "2026-10-04T12:00:00Z",
                "last_at": "2026-10-04T12:01:00Z",
            }
        }
    }

    def scanned(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr("django_ergo.plugins.orca.subprocess.run", scanned)
    ctx = WorkerContext(bot, worker)
    plugin.scan_usage(ctx, settled=True)
    [row] = AgentUsage.objects.all()
    assert (
        row.agent,
        row.model,
        row.input_tokens,
        row.output_tokens,
        row.reasoning_tokens,
    ) == (
        "codex",
        "gpt-6-codex",
        70,
        25,
        5,
    )
    assert calls[0][0][:4] == ["ssh", "-o", "BatchMode=yes", "devhost"]
    assert calls[0][1]["input"].startswith('"""Read coding-agent session files')

    payload["models"]["gpt-6-codex"]["input"] = 90
    plugin.scan_usage(ctx, settled=True)
    row.refresh_from_db()
    assert row.input_tokens == 90

    monkeypatch.setattr(
        "django_ergo.plugins.orca.subprocess.run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="offline"
        ),
    )
    plugin.scan_usage(ctx, settled=True)
    row.refresh_from_db()
    assert row.input_tokens == 90


@pytest.mark.django_db
def test_orca_usage_scan_runs_locally_when_files_host_is_empty(tmp_path, monkeypatch):
    import subprocess

    from django_ergo.bots.workers import WorkerContext
    from tests.test_bot_plugins import orca_bot

    bot, _, plugin = orca_bot(
        tmp_path, config="environment: '', files_host: '', executable: orca-test"
    )
    user = get_user_model().objects.create(username="local-usage")
    session = async_to_sync(bot.main_session)(user)
    worker = Worker.objects.create(
        session=session,
        bot_name=bot.name,
        title="omp: Count tokens",
        function="orca:watch",
        state={"agent": "omp", "worktree": "/work/project"},
    )
    calls = []

    def scanned(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout='{"models":{}}', stderr="")

    monkeypatch.setattr("django_ergo.plugins.orca.subprocess.run", scanned)
    plugin.scan_usage(WorkerContext(bot, worker), settled=True)
    assert calls[0][:2] == ["python3", "-"]
