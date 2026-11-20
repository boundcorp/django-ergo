"""Regression checks for worker login admission and launch identity."""

from datetime import UTC
from datetime import datetime
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from django_ergo.bots.providers import Provider
from django_ergo.bots.providers import Providers
from django_ergo.bots.routing import coding_login_error
from django_ergo.bots.routing import coding_login_store
from django_ergo.bots.routing import pick_agent
from django_ergo.plugins.orca import OrcaPlugin
from django_ergo.plugins.orca import claude_trust_blocked
from django_ergo.plugins.orca_trust import seed_trust


@pytest.mark.parametrize(
    ("agent", "provider", "store"),
    [
        ("claude", "anthropic", "claude-cli"),
        ("codex", "openai-codex", "codex-cli"),
        ("omp", "anthropic", "omp/anthropic"),
        ("omp", "xai-oauth", "omp/xai-oauth"),
    ],
)
def test_agent_uses_its_own_login_store(agent, provider, store):
    assert coding_login_store(agent, provider) == store


@pytest.mark.parametrize(
    ("age", "state", "expected"),
    [
        (3600, "ok", ""),
        (4 * 3600, "ok", "login health is stale, run orca_refresh_limits"),
        (3600, "expired", "Claude CLI login expired; run claude auth login"),
    ],
)
def test_account_login_freshness_and_named_error(monkeypatch, age, state, expected):
    now = datetime.now(UTC).timestamp()
    checked = datetime.fromtimestamp(now - age, UTC)
    db = MagicMock()
    db.introspection.table_names.return_value = ["ergo_bot_devbox_accountlogin"]
    db.cursor.return_value.__enter__.return_value.fetchone.return_value = (
        state,
        "refresh expired",
        "Claude CLI login expired",
        "claude auth login",
        checked,
    )
    with patch("django.db.connection", db):
        assert coding_login_error("claude-cli") == expected
    db.cursor.return_value.__enter__.return_value.execute.assert_called_once()
    assert db.cursor.return_value.__enter__.return_value.execute.call_args.args[1] == [
        "claude-cli"
    ]


def test_missing_login_table_is_unknown():
    db = MagicMock()
    db.introspection.table_names.return_value = []
    with patch("django.db.connection", db):
        assert "stale" in coding_login_error("codex-cli")


@pytest.mark.django_db
def test_old_successful_usage_sync_cannot_admit_worker(monkeypatch):
    from django_ergo.conversation.models import UsageSync

    now = datetime.now(UTC).timestamp()
    monkeypatch.setattr(Provider, "available", property(lambda self: True))
    providers = Providers.from_dict(
        {
            "providers": {
                "xai-oauth": {
                    "type": "omp",
                    "transport": "cli",
                    "account": "xai-oauth",
                }
            },
            "agents": {
                "medium": [
                    {
                        "agent": "omp",
                        "provider": "xai-oauth",
                        "model": "xai-oauth/grok-4.7",
                    }
                ]
            },
        }
    )
    monkeypatch.setattr(
        "django_ergo.bots.routing.coding_login_error", lambda _store: ""
    )
    UsageSync.objects.create(
        pk=1, accounts={"xai-oauth": {"error": "", "fetched_at": now - 4 * 3600}}
    )
    with pytest.raises(
        ValueError, match="login health is stale, run orca_refresh_limits"
    ):
        pick_agent(providers, "medium")
    UsageSync.objects.filter(pk=1).update(
        accounts={"xai-oauth": {"error": "", "fetched_at": now - 3600}}
    )
    assert pick_agent(providers, "medium").agent == "omp"


@pytest.mark.django_db
def test_native_agent_uses_cli_health_even_if_omp_store_failed(monkeypatch):
    from django_ergo.conversation.models import UsageSync

    monkeypatch.setattr(Provider, "available", property(lambda self: True))
    monkeypatch.setattr(
        "django_ergo.bots.routing.coding_login_error", lambda _store: ""
    )
    providers = Providers.from_dict(
        {
            "providers": {
                "anthropic": {
                    "type": "claude",
                    "transport": "cli",
                    "models": ["claude-sonnet-5-5"],
                }
            },
            "agents": {
                "medium": [
                    {
                        "agent": "claude",
                        "provider": "anthropic",
                        "model": "claude-sonnet-5-5",
                    }
                ]
            },
        }
    )
    UsageSync.objects.create(
        pk=1, accounts={"anthropic": {"error": "omp login expired"}}
    )
    assert pick_agent(providers, "medium").agent == "claude"


@pytest.mark.parametrize(
    ("agent", "model", "store"),
    [
        ("claude", "", "claude-cli"),
        ("codex", "", "codex-cli"),
        ("omp", "xai-oauth/grok-4.7", "omp/xai-oauth"),
    ],
)
def test_explicit_orca_start_checks_login_before_side_effects(
    monkeypatch, agent, model, store
):
    from django_ergo.bots.agents import AgentSpec

    plugin = OrcaPlugin.__new__(OrcaPlugin)
    plugin.scoped_repositories = None
    monkeypatch.setattr(
        "django_ergo.bots.routing.coding_login_error", lambda store: "login expired"
    )
    with pytest.raises(ValueError, match=f"{store}: login expired"):
        plugin.start_agent(None, AgentSpec("Fix", "worktree", agent, model))


def _omp_session(path, cwd, model, *, provider=None, fallback=False, mtime=None):  # noqa: PLR0913
    import json
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    provider_id, _, model_id = model.partition("/")
    rows = [
        {"type": "title", "title": "secret brief"},
        {"type": "session", "id": "s", "cwd": cwd},
        {"type": "model_change", "model": model, "resolvedModelIsFallback": fallback},
        {"type": "message", "message": {"role": "user", "content": "secret"}},
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "provider": provider or provider_id,
                "model": model_id,
                "content": "secret answer",
            },
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_omp_session_log_reports_provider_and_model_for_this_launch(
    tmp_path, monkeypatch
):
    import time

    agent = tmp_path / ".omp" / "agent"
    sessions = agent / "sessions"
    cwd = "/home/dev/orca/workspaces/repo/branch"
    now = time.time()
    # This launch: omp answered from another provider under the same model id.
    _omp_session(
        sessions / "-orca-workspaces-repo-branch" / "new.jsonl",
        cwd,
        "openai-codex/gpt-6.1-terra",
        provider="github-copilot",
    )
    # An earlier run in the same worktree and a concurrent run elsewhere.
    _omp_session(
        sessions / "-orca-workspaces-repo-branch" / "old.jsonl",
        cwd,
        "fireworks/kimi-k3",
        mtime=now - 3600,
    )
    _omp_session(sessions / "-other" / "x.jsonl", "/elsewhere", "fireworks/kimi-k3")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))
    plugin = OrcaPlugin.__new__(OrcaPlugin)
    plugin.files_host = ""
    plugin.timeout = 10
    used = plugin.omp_session_models({"path": cwd, "launched_at": now - 60})
    assert used == {
        "models": ["openai-codex/gpt-6.1-terra", "github-copilot/gpt-6.1-terra"],
        "fallback": False,
    }
    assert "secret" not in str(used)
    assert plugin.omp_session_models({"path": "/none", "launched_at": now}) == {
        "models": [],
        "fallback": False,
    }


def test_claude_trust_seed_preserves_settings_and_rejects_outside_path(tmp_path):
    import json

    root = tmp_path / "workspaces"
    worktree = root / "repo" / "branch"
    worktree.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    target = home / ".claude.json"
    target.write_text(
        json.dumps({"other": [1, 2], "projects": {str(worktree): {"note": "keep"}}})
    )
    seed_trust(str(worktree), root=root, home=home)
    saved = json.loads(target.read_text())
    assert saved == {
        "other": [1, 2],
        "projects": {str(worktree): {"note": "keep", "hasTrustDialogAccepted": True}},
    }
    with pytest.raises(ValueError, match="outside Orca workspaces"):
        seed_trust(str(outside), root=root, home=home)
    assert json.loads(target.read_text()) == saved


@pytest.mark.parametrize(
    ("lines", "blocked"),
    [
        (["Do you trust the files in this folder?"], True),
        # Claude Code 2.1.x's dialog.
        (
            [
                " Quick safety check: Is this a project you created or one you trust?",
                " \u276f 1. Yes, I trust this folder",
                "   2. No, exit",
            ],
            True,
        ),
        (["claude exited", "dev@devbox:~/worktree$ "], True),
        (["Claude Code", "Thinking about the task"], False),
    ],
)
def test_claude_trust_prompt_and_shell_return_detection(lines, blocked):
    assert claude_trust_blocked(lines) is blocked
