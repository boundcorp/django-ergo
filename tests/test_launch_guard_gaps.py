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


def test_omp_catalog_display_name_resolves_provider_and_model(tmp_path, monkeypatch):
    import json
    import sqlite3

    directory = tmp_path / ".omp" / "agent"
    directory.mkdir(parents=True)
    with sqlite3.connect(directory / "models.db") as db:
        db.execute(
            "CREATE TABLE model_cache (provider_id TEXT, models TEXT, updated_at INTEGER)"
        )
        db.executemany(
            "INSERT INTO model_cache VALUES (?, ?, 1)",
            [
                ("xai-oauth", json.dumps([{"id": "grok-4.7", "name": "Grok 4.7 🙈"}])),
                ("anthropic", json.dumps([{"id": "grok-4.7", "name": "Other Grok"}])),
            ],
        )
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(directory))
    plugin = OrcaPlugin.__new__(OrcaPlugin)
    plugin.files_host = ""
    plugin.timeout = 10
    assert plugin.omp_catalog_name("xai-oauth/grok-4.7") == "Grok 4.7 🙈"
    with pytest.raises(ValueError, match="missing or ambiguous"):
        plugin.omp_catalog_name("xai-oauth/unknown")


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
        (["claude exited", "dev@devbox:~/worktree$ "], True),
        (["Claude Code", "Thinking about the task"], False),
    ],
)
def test_claude_trust_prompt_and_shell_return_detection(lines, blocked):
    assert claude_trust_blocked(lines) is blocked
