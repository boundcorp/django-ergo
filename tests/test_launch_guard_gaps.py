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
from django_ergo.plugins.orca import agent_blocked
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


CLAUDE_TRUST = [
    " Quick safety check: Is this a project you created or one you trust?",
    " \u276f 1. Yes, I trust this folder",
    "   2. No, exit",
    " Enter to confirm \u00b7 Esc to cancel",
]
# A real devbox terminal after the agent exited (zsh with a right prompt).
DEVBOX_SHELL = [
    "\x1b[34m~/orca/workspaces/holyword/tiktok-sdk-plan\x1b[0m <leewardbound/tiktok-sdk-plan > (16:41:07)",
    "(python) \u00bb                                                  [home:default]",
    "",
]
# A working Claude Code TUI: its footer, not a prompt, is the last line.
CLAUDE_WORKING = [
    "  \u23bf  Updated tests/test_launch_guard_gaps.py",
    "\u273b Shenaniganing\u2026 (4m 30s \u00b7 \u2193 11.9k tokens)",
    "\u2500" * 40,
    "\u276f",
    "\u2500" * 40,
    "  \u23f5\u23f5 bypass permissions on \u00b7 1 shell \u00b7 esc to interrupt",
]
LONG_AGO = 1.0  # launched long before the start grace


@pytest.mark.parametrize(
    ("lines", "checked_in", "launched_at", "reason"),
    [
        (CLAUDE_TRUST, False, None, "folder trust prompt"),
        (["Do you trust the files in this folder?"], False, None, "folder trust"),
        (["> Do you trust the contents of this directory?"], False, None, "trust"),
        # An agent that checked in is past the dialog, even if it quotes it.
        (CLAUDE_TRUST + CLAUDE_WORKING, True, LONG_AGO, ""),
        (DEVBOX_SHELL, True, LONG_AGO, "exited to a shell prompt ((python) \u00bb"),
        (DEVBOX_SHELL, False, None, "exited to a shell prompt"),
        (["claude exited", "dev@devbox:~/worktree$ "], True, LONG_AGO, "shell"),
        # The shell typing the agent's command, and a bare prompt right at launch.
        (["(python) \u00bb claude --resume  [home:default]"], False, LONG_AGO, ""),
        (DEVBOX_SHELL, False, "now", ""),
        (CLAUDE_WORKING, True, LONG_AGO, ""),
        (["omp \u2502 45%", "\u03c0 / Grok 4.7 / \u2026 \u2502 $"], True, LONG_AGO, ""),
    ],
)
def test_agent_blocked_by_trust_prompt_or_exit_to_shell(
    lines, checked_in, launched_at, reason
):
    import time

    if launched_at == "now":
        launched_at = time.time()
    found = agent_blocked(lines, checked_in=checked_in, launched_at=launched_at)
    assert (reason in found) if reason else found == ""
    assert bool(found) is bool(reason)


class _BlockedPlugin(OrcaPlugin):
    """check()'s Orca calls for one unsettled worker with this terminal tail."""

    def __init__(self, tail, dispatch):
        self.tail = tail
        self.dispatch_row = dispatch
        self.calls = []
        self.scoped_repositories = None

    def cli_json(self, args):
        self.calls.append(args[:2])
        if args[:2] == ["orchestration", "worker-show"]:
            return {
                "dispatch": self.dispatch_row,
                "worker": {"agent_terminal_handle": "term_1", "state": "ready"},
                "observation": {"status": "running"},
            }
        if args[:2] == ["terminal", "read"]:
            return {"terminal": {"tail": self.tail}}
        if args[:2] == ["orchestration", "inbox"]:
            return {"messages": []}
        if args[:2] == ["terminal", "show"]:
            return {"terminal": {"connected": True, "paneRuntimeId": 3}}
        return {}

    def record_activity(self, *_args):
        pass

    def scan_usage(self, *_args, **_kwargs):
        pass


@pytest.mark.parametrize("agent", ["claude", "codex", "omp"])
@pytest.mark.parametrize(
    ("tail", "dispatch", "error"),
    [
        (DEVBOX_SHELL, {"status": "dispatched", "last_heartbeat_at": "x"}, "shell"),
        (CLAUDE_TRUST, {"status": "dispatched"}, "folder trust prompt"),
    ],
)
def test_check_fails_a_dead_or_trust_blocked_worker_for_every_agent(
    monkeypatch, agent, tail, dispatch, error
):
    from types import SimpleNamespace

    from django_ergo.plugins import orca

    monkeypatch.setattr(orca.instruction_protocol, "reconcile", lambda *a: None)
    plugin = _BlockedPlugin(tail, dispatch)
    ctx = SimpleNamespace(state={}, stopping=False, progress=lambda *_: None)
    handle = {"dispatch": "ctx_1", "run": "run_1", "agent": agent, "launched_at": 1.0}
    with pytest.raises(RuntimeError, match=f"The {agent} agent stopped .*{error}"):
        plugin.check(ctx, handle)
    assert ["orchestration", "worker-stop"] in plugin.calls


def test_check_keeps_a_working_worker_running(monkeypatch):
    from types import SimpleNamespace

    from django_ergo.plugins import orca

    monkeypatch.setattr(orca.instruction_protocol, "reconcile", lambda *a: None)
    plugin = _BlockedPlugin(
        CLAUDE_WORKING, {"status": "dispatched", "last_heartbeat_at": "x"}
    )
    ctx = SimpleNamespace(state={}, stopping=False, progress=lambda *_: None)
    handle = {
        "dispatch": "ctx_1",
        "run": "run_1",
        "agent": "claude",
        "launched_at": 1.0,
    }
    assert plugin.check(ctx, handle).status == "running"
    assert ["orchestration", "worker-stop"] not in plugin.calls
