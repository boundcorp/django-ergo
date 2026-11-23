from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model

from django_ergo.bots.agents import AgentSpec
from django_ergo.bots.agents import agent_toolkit
from django_ergo.bots.providers import Providers
from django_ergo.bots.registry import BotRegistry
from django_ergo.bots.routing import record_usage_windows
from django_ergo.bots.tools import ToolContext
from django_ergo.bots.workers import worker_toolkit
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import ThreadMessage
from django_ergo.conversation.models import UsageSync
from django_ergo.conversation.models import Worker
from django_ergo.plugins.orca_scope import admitted_providers
from django_ergo.plugins.orca_scope import host_check
from tests.test_bot_plugins import orca_bot
from tests.test_bot_workers import workers  # noqa: F401
from tests.test_bots import make_bot

CATALOGS = {
    "anthropic": [
        {
            "id": "claude-sonnet-5-5",
            "name": "Claude Sonnet 5.5",
            "kind": "chat",
            "cost": {"input": 1, "output": 2},
        },
    ],
    "openai-codex": [
        {
            "id": "gpt-6-sol",
            "name": "GPT 6 Sol",
            "kind": "chat",
            "cost": {"input": 1, "output": 2},
        },
    ],
    "xai-oauth": [
        {
            "id": "grok-test",
            "name": "Grok Test",
            "kind": "chat",
            "cost": {"input": 1, "output": 2},
        },
    ],
}
SUBSCRIPTIONS = ["anthropic", "openai-codex", "xai-oauth"]


def provider_config():
    return {
        "providers": {
            "claude": {
                "type": "claude",
                "transport": "cli",
                "models": ["claude-sonnet-5-5"],
            },
            "chatgpt": {"type": "openai", "transport": "cli", "models": ["gpt-6-sol"]},
            "paid": {"type": "openai", "models": ["gpt-6-sol"]},
        },
        "agents": {
            "medium": [
                {"agent": "claude", "provider": "claude", "model": "claude-sonnet-5-5"},
                {
                    "agent": "codex",
                    "provider": "chatgpt",
                    "model": "gpt-6-sol",
                    "effort": "medium",
                },
            ],
        },
        "routing": {
            "limits": [
                {"provider": "claude", "window": "five_hour", "max_used": 85},
                {"provider": "chatgpt", "window": "weekly", "max_used": 80},
            ]
        },
    }


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def execution_host(tmp_path, monkeypatch):
    """A real, local, read-only catalog/credential check; never a provider call."""
    home = tmp_path / "host"
    agent = home / ".omp" / "agent"
    agent.mkdir(parents=True)
    with sqlite3.connect(agent / "models.db") as db:
        db.execute(
            "CREATE TABLE model_cache (provider_id TEXT, models TEXT, updated_at INTEGER)"
        )
        db.executemany(
            "INSERT INTO model_cache VALUES (?, ?, 1)",
            [(p, json.dumps(m)) for p, m in CATALOGS.items()],
        )
    with sqlite3.connect(agent / "agent.db") as db:
        db.execute(
            "CREATE TABLE auth_credentials (provider TEXT, credential_type TEXT, disabled_cause TEXT)"
        )
        db.executemany(
            "INSERT INTO auth_credentials VALUES (?, 'oauth', NULL)",
            [(p,) for p in SUBSCRIPTIONS],
        )
    binary = home / "bin"
    binary.mkdir()
    omp = binary / "omp"
    omp.write_text("#!/bin/sh\nexit 99\n")
    omp.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))
    monkeypatch.setenv("PATH", f"{binary}{os.pathsep}{os.environ['PATH']}")
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    return agent


class Runner:
    """Only the external Orca executable is replaced; git, checks and pinning run locally."""

    def __init__(self, root):
        self.root = root
        self.calls = []
        self.paths = []
        self.fail = None
        self.status = "dispatched"
        self.messages = []
        self.starts = 0
        self.checkout_change = ""

    def respond(self, args):
        self.calls.append(args)
        command = tuple(args[:2])
        if command == self.fail:
            msg = "runner refused request"
            raise ValueError(msg)
        if command == ("worktree", "create"):
            path = self.root / f"checkout-{len(self.paths) + 1}"
            path.mkdir()
            git(path, "init", "-q")
            git(
                path,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.test",
                "commit",
                "--allow-empty",
                "-qm",
                "base",
            )
            git(path, "update-ref", "refs/remotes/origin/main", "HEAD")
            if self.checkout_change == "dirty":
                (path / "unexpected.txt").write_text("setup must not run")
            elif self.checkout_change == "ahead":
                git(
                    path,
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.test",
                    "commit",
                    "--allow-empty",
                    "-qm",
                    "unexpected",
                )
            self.paths.append(path)
            return {"worktree": {"id": f"wt-{len(self.paths)}", "path": str(path)}}
        if command == ("worktree", "show"):
            selector = args[args.index("--worktree") + 1]
            index = int(selector.rsplit("-", 1)[1]) - 1
            return {
                "worktree": {
                    "id": selector.removeprefix("id:"),
                    "path": str(self.paths[index]),
                }
            }
        if command == ("orchestration", "worker-start"):
            self.starts += 1
            return {"dispatchId": f"dispatch-{self.starts}"}
        if command == ("terminal", "read"):
            launched = self.commands("orchestration", "worker-start")[-1]
            if launched[launched.index("--agent") + 1] != "omp":
                return {"terminal": {"tail": []}}
            import yaml

            roles = yaml.safe_load((self.paths[-1] / ".omp/config.yml").read_text())[
                "modelRoles"
            ]
            return {"terminal": {"tail": [roles["default"].split(":")[0]]}}
        fixed = {
            ("terminal", "create"): {"terminal": {"handle": "mailbox-1"}},
            ("terminal", "show"): {"terminal": {"connected": True, "paneRuntimeId": 7}},
            ("terminal", "send"): {"sent": True},
            ("orchestration", "run-create"): {"run": {"id": "run-1"}},
            ("orchestration", "task-create"): {"task": {"id": "task-1"}},
            ("orchestration", "worker-show"): {
                "dispatch": {"status": self.status},
                "worker": {"state": "running", "agent_terminal_handle": "agent-1"},
                "observation": {"status": "alive"},
            },
            ("orchestration", "inbox"): {"messages": self.messages},
            ("orchestration", "worker-read"): {"terminal": {"tail": []}},
            ("orchestration", "reply"): {"sent": True},
            ("orchestration", "worker-stop"): {"stopped": True},
            ("orchestration", "worker-release"): {"released": True},
        }
        return fixed[command]

    def commands(self, *command):
        return [args for args in self.calls if args[:2] == list(command)]


@pytest.fixture
def scoped(tmp_path, monkeypatch, execution_host, workers):  # noqa: F811
    # These scope tests exercise catalog/routing policy; login-store admission
    # has its own tests and ergo-bots' table is not installed in this fixture.
    monkeypatch.setattr(
        "django_ergo.bots.routing.coding_login_error", lambda _store: ""
    )
    UsageSync.objects.update_or_create(
        pk=1,
        defaults={
            "accounts": {
                provider: {"error": "", "fetched_at": time.time()}
                for provider in SUBSCRIPTIONS
            }
        },
    )
    bot, _, plugin = orca_bot(
        tmp_path,
        config="executable: orca-test, files_host: '', scoped_repositories: {demo: 'name:demo'}, agents: [claude, codex, omp]",
    )
    # Test worktrees live under pytest's /tmp root, outside Orca's trusted root.
    monkeypatch.setattr(plugin, "seed_claude_trust", lambda _worktree, _path: None)
    registry = BotRegistry()
    registry.providers = Providers.from_dict(provider_config())
    registry.add(bot)
    user = get_user_model().objects.create(username="scoped")
    session = async_to_sync(bot.main_session)(user)
    ctx = ToolContext(bot=bot, session=session, user=user)
    runner = Runner(tmp_path)
    real_run = subprocess.run

    def run(argv, **kwargs):
        if argv[0] != "orca-test":
            return real_run(argv, **kwargs)
        try:
            result = runner.respond(argv[1:])
        except ValueError as exc:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr=str(exc))
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"ok": True, "result": result}), stderr=""
        )

    monkeypatch.setattr("django_ergo.plugins.orca.subprocess.run", run)
    return SimpleNamespace(
        bot=bot,
        plugin=plugin,
        session=session,
        ctx=ctx,
        kit=agent_toolkit(bot, ctx),
        runner=runner,
        scheduled=workers,
    )


def start(scoped, **kwargs):
    return json.loads(
        scoped.kit.execute_tool(
            "ergo_agent_start",
            {"brief": "Fix the footer", "workspace": "demo", **kwargs},
        )
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("tier", ["", "medium"])
@pytest.mark.parametrize(
    "selector",
    [
        {"workspace": "/tmp/arbitrary"},
        {"workspace": "path:/tmp/arbitrary"},
        {"workspace": "id:another-worktree"},
        {"workspace": "name:demo"},
        {"agent": "arbitrary"},
        {"model": "paid/gpt-6-sol"},
        {"model": "unknown/gpt-6-sol"},
        {"model": "gpt-6-sol"},
        {"model": "anthropic/not-listed"},
    ],
)
def test_generic_start_rejects_invalid_selectors_before_side_effects(
    scoped, tier, selector
):
    with pytest.raises(ValueError, match="Scoped|private"):
        start(scoped, tier=tier, **selector)
    assert scoped.runner.calls == []
    assert not Worker.objects.exists()
    assert scoped.scheduled == []


@pytest.mark.django_db(transaction=True)
def test_default_medium_routes_safe_subscription_and_pins_all_roles(scoped):
    record_usage_windows(
        "claude", {"five_hour": {"used": 99, "resets_at": time.time() + 3600}}
    )
    started = start(scoped)
    assert started["agent"] == {
        "agent": "codex",
        "model": "gpt-6-sol",
        "effort": "medium",
        "tier": "medium",
    }
    [created] = scoped.runner.commands("worktree", "create")
    assert created[created.index("--repo") + 1] == "name:demo"
    assert created[created.index("--base-branch") + 1] == "origin/main"
    assert created[created.index("--setup") + 1] == "skip"
    assert "--no-parent" in created
    [launched] = scoped.runner.commands("orchestration", "worker-start")
    assert launched[launched.index("--agent") + 1] == "codex"
    assert launched[launched.index("--worktree") + 1] == "id:wt-1"
    assert launched[launched.index("--model") + 1] == "gpt-6-sol"
    assert launched[launched.index("--effort") + 1] == "medium"
    # Filtering is a copy, not a destructive edit of global routing.
    assert len(scoped.bot.providers.routing.agents["medium"]) == 2
    worker = Worker.objects.get(pk=started["id"])
    assert worker.args["handle"]["worktree"] == "id:wt-1"
    assert worker.state["dispatch_receipt"]["selected"]["model"] == "gpt-6-sol"


@pytest.mark.django_db(transaction=True)
def test_exhausted_subscriptions_never_fall_back_to_paid_api(scoped):
    for provider, window in (("claude", "five_hour"), ("chatgpt", "weekly")):
        record_usage_windows(
            provider, {window: {"used": 100, "resets_at": time.time() + 3600}}
        )
    with pytest.raises(ValueError, match="No safe subscription"):
        start(scoped, tier="medium")
    assert scoped.runner.calls == []
    assert not Worker.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_explicit_canonical_model_is_admitted_without_tier(scoped):
    started = start(scoped, model="anthropic/claude-sonnet-5-5", agent="claude")
    assert started["agent"]["model"] == "claude-sonnet-5-5"
    assert started["agent"]["tier"] == ""
    [launched] = scoped.runner.commands("orchestration", "worker-start")
    assert launched[launched.index("--model") + 1] == "claude-sonnet-5-5"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("root_only", [True, False])
def test_scope_permissions_and_no_raw_orca_tools(scoped, root_only):
    scoped.plugin.root_only = root_only
    assert scoped.plugin._tools() == []
    assert scoped.plugin.manager.agents == ("claude", "codex", "omp")
    assert scoped.kit.has_tool("ergo_agent_start")
    thread = ConversationSession.objects.create(
        user=scoped.session.user,
        bot_name=scoped.bot.name,
        metadata={"bot_role": "thread"},
        parent=scoped.session,
    )
    ctx = ToolContext(bot=scoped.bot, session=thread, user=thread.user)
    kit = agent_toolkit(scoped.bot, ctx)
    assert kit.has_tool("ergo_agent_start") is (not root_only)
    for toolkit in scoped.plugin.toolkits(ctx):
        assert not any(
            toolkit.has_tool(name)
            for name in (
                "orca_read",
                "orca_run",
                "orca_start_worker",
                "orca_attach",
                "orca_upload",
            )
        )
    if root_only:
        with pytest.raises(ValueError, match="authorized chat"):
            scoped.plugin.start_agent(
                ctx,
                AgentSpec(
                    "Fix",
                    "demo",
                    agent="omp",
                    model="anthropic/claude-sonnet-5-5",
                ),
            )
        assert scoped.runner.calls == []
    else:
        result = json.loads(
            kit.execute_tool(
                "ergo_agent_start",
                {
                    "brief": "Fix from thread",
                    "workspace": "demo",
                },
            )
        )
        assert Worker.objects.get(pk=result["id"]).session_id == thread.pk


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("sibling", ["bash", "local_shell"])
def test_scope_cannot_load_with_unrestricted_shell_sibling(tmp_path, sibling):
    with pytest.raises(ValueError, match="cannot be combined"):
        make_bot(
            tmp_path,
            yaml_text=f"""
            name: restricted
            plugins:
              - name: orca
                scoped_repositories: {{demo: 'name:demo'}}
              - name: {sibling}
        """,
            name="restricted",
        )


@pytest.mark.django_db(transaction=True)
def test_other_chat_cannot_list_answer_stop_or_cancel_job(scoped):
    started = start(scoped)
    other = ConversationSession.objects.create(
        user=scoped.session.user,
        bot_name=scoped.bot.name,
        metadata={"bot_role": "main"},
    )
    ctx = ToolContext(bot=scoped.bot, session=other, user=other.user)
    agents = agent_toolkit(scoped.bot, ctx)
    workers_kit = worker_toolkit(scoped.bot, ctx)
    before = list(scoped.runner.calls)
    assert json.loads(workers_kit.execute_tool("ergo_worker_list", {})) == []
    for toolkit, name, extra in (
        (agents, "ergo_agent_reply", {"question_id": "q", "answer": "yes"}),
        (agents, "ergo_agent_stop", {}),
        (workers_kit, "ergo_worker_cancel", {}),
    ):
        with pytest.raises(ValueError, match="in this chat"):
            toolkit.execute_tool(name, {"worker_id": started["id"], **extra})
    assert scoped.runner.calls == before
    assert Worker.objects.get(pk=started["id"]).status == "queued"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "failure",
    [
        ("worktree", "create"),
        ("orchestration", "worker-start"),
    ],
)
def test_creation_or_launch_failure_never_schedules_untracked_watcher(scoped, failure):
    scoped.runner.fail = failure
    with pytest.raises(ValueError, match="runner refused request") as error:
        start(scoped)
    assert not Worker.objects.exists()
    assert scoped.scheduled == []
    assert scoped.runner.starts == 0
    if failure[0] == "worktree":
        assert scoped.runner.paths == []
    else:
        assert "retained worktree id:wt-1" in str(error.value)
        assert str(scoped.runner.paths[0]) in str(error.value)
        assert scoped.runner.paths[0].is_dir()


@pytest.mark.django_db(transaction=True)
def test_watcher_persistence_failure_stops_launched_dispatch(scoped, monkeypatch):
    from django_ergo.bots.workers import WorkerStarter

    def fail(*args, **kwargs):
        msg = "watcher persistence unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(WorkerStarter, "start", fail)
    with pytest.raises(RuntimeError, match="watcher persistence unavailable"):
        start(scoped)
    assert scoped.runner.starts == 1
    [stop] = scoped.runner.commands("orchestration", "worker-stop")
    assert stop[stop.index("--dispatch") + 1] == "dispatch-1"
    assert not Worker.objects.exists()
    assert scoped.scheduled == []
    assert scoped.runner.paths[0].is_dir()


@pytest.mark.django_db(transaction=True)
def test_question_reply_completion_are_durable_and_next_start_reuses_mailbox(scoped):
    from django_ergo.bots import workers as w

    started = start(scoped)
    worker_id = started["id"]
    scoped.runner.messages = [
        {
            "id": "q-1",
            "run_id": "run-1",
            "type": "question",
            "subject": "Which color?",
            "body": "Blue or red?",
            "payload": json.dumps({"dispatchId": "dispatch-1"}),
            "sequence": 1,
        }
    ]
    assert w.run(worker_id, scoped.bot.registry) == "running"
    assert w.run(worker_id, scoped.bot.registry) == "running"
    messages = ThreadMessage.objects.filter(metadata__worker=worker_id)
    assert sum("Blue or red?" in message.text for message in messages) == 1
    # Recreate the caller toolkit: replies are in the DB, not a tool instance.
    kit = agent_toolkit(scoped.bot, scoped.ctx)
    answer = {"worker_id": worker_id, "question_id": "q-1", "answer": "Blue"}
    assert "queued" in kit.execute_tool("ergo_agent_reply", answer)
    assert scoped.runner.commands("orchestration", "reply") == []
    assert "already queued" in kit.execute_tool("ergo_agent_reply", answer)
    assert w.run(worker_id, scoped.bot.registry) == "running"
    [reply] = scoped.runner.commands("orchestration", "reply")
    assert reply[reply.index("--body") + 1] == "Blue"
    assert reply[reply.index("--from") + 1] == "mailbox-1"
    assert reply[reply.index("--run") + 1] == "run-1"
    scoped.runner.status = "completed"
    scoped.runner.messages.append(
        {
            "id": "done-1",
            "run_id": "run-1",
            "type": "worker_done",
            "subject": "Footer fixed",
            "body": "The footer is blue.",
            "payload": json.dumps({"dispatchId": "dispatch-1"}),
            "sequence": 2,
        }
    )
    assert w.run(worker_id, scoped.bot.registry) == "completed"
    assert Worker.objects.get(pk=worker_id).result["report"] == "The footer is blue."
    assert sum("The footer is blue." in message.text for message in messages.all()) == 1
    assert len(scoped.runner.commands("orchestration", "worker-release")) == 1
    second = start(scoped)
    assert second["id"] != worker_id
    assert len(scoped.runner.paths) == 2
    assert len(scoped.runner.commands("terminal", "create")) == 1
    assert len(scoped.runner.commands("orchestration", "run-create")) == 1
    assert len(scoped.runner.commands("orchestration", "reply")) == 1


def test_host_check_reads_only_local_catalog_and_subscription_metadata(execution_host):
    metadata = host_check()
    assert metadata["omp"] is True
    assert metadata["catalogs"] == CATALOGS
    assert metadata["subscriptions"] == sorted(SUBSCRIPTIONS)
    # The fixture has no credential payload column: inspection must not need secrets.
    with sqlite3.connect(execution_host / "agent.db") as db:
        db.execute("DELETE FROM auth_credentials")
    assert host_check()["subscriptions"] == []


@pytest.mark.parametrize(
    ("credential", "disabled", "key"),
    [
        ("api_key", None, ""),
        ("oauth", "revoked", ""),
        ("oauth", None, "ANTHROPIC_API_KEY"),
    ],
)
def test_host_check_rejects_paid_disabled_or_ambiguous_credentials(
    execution_host,
    monkeypatch,
    credential,
    disabled,
    key,
):
    with sqlite3.connect(execution_host / "agent.db") as db:
        db.execute("DELETE FROM auth_credentials WHERE provider = 'anthropic'")
        db.execute(
            "INSERT INTO auth_credentials VALUES ('anthropic', ?, ?)",
            (credential, disabled),
        )
        if credential == "api_key":
            db.execute(
                "INSERT INTO auth_credentials VALUES ('anthropic', 'oauth', NULL)"
            )
    if key:
        monkeypatch.setenv(key, "test-not-a-real-key")
    assert "anthropic" not in host_check()["subscriptions"]


def test_host_check_has_no_network_fallback_for_missing_cache(execution_host):
    (execution_host / "models.db").unlink()
    with pytest.raises(sqlite3.OperationalError):
        host_check()
    assert not (execution_host / "models.db").exists()


def test_host_checkout_check_detects_dirty_and_wrong_base(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-q")
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "commit",
        "--allow-empty",
        "-qm",
        "base",
    )
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    initial = host_check(str(root))
    assert initial["clean"] is True and initial["head"] == initial["base"]
    untracked = root / "untracked.txt"
    untracked.write_text("not clean")
    assert host_check(str(root))["clean"] is False
    untracked.unlink()
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "commit",
        "--allow-empty",
        "-qm",
        "ahead",
    )
    ahead = host_check(str(root))
    assert ahead["clean"] is True and ahead["head"] != ahead["base"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "patch",
    [
        {"enabled": False},
        {"disabled": True},
        {"quarantined": True},
        {"kind": "embedding"},
        {"kind": None},
        {"cost": {}},
        {"cost": {"input": 0, "output": 0}},
        {"pricingStatus": "unpriced"},
        {"supportsTools": False},
    ],
)
def test_generic_start_rejects_ineligible_cached_model(scoped, execution_host, patch):
    raw = {**CATALOGS["anthropic"][0], **patch}
    with sqlite3.connect(execution_host / "models.db") as db:
        db.execute(
            "UPDATE model_cache SET models = ? WHERE provider_id = 'anthropic'",
            (json.dumps([raw]),),
        )
    with pytest.raises(ValueError, match="Scoped model"):
        start(scoped, model="anthropic/claude-sonnet-5-5", tier="medium")
    assert scoped.runner.calls == []
    assert not Worker.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_generic_start_denies_catalog_without_active_oauth(scoped, execution_host):
    with sqlite3.connect(execution_host / "agent.db") as db:
        db.execute("DELETE FROM auth_credentials")
    with pytest.raises(ValueError, match="tier|subscription"):
        start(scoped)
    with pytest.raises(ValueError, match="Scoped model"):
        start(scoped, model="anthropic/claude-sonnet-5-5")
    assert scoped.runner.calls == []
    assert scoped.scheduled == []


@pytest.mark.parametrize("subscriptions", [None, []])
def test_catalog_alone_never_authorizes_subscription(subscriptions):
    metadata = {"omp": True, "catalogs": CATALOGS}
    if subscriptions is not None:
        metadata["subscriptions"] = subscriptions
    filtered, allowed = admitted_providers(
        Providers.from_dict(provider_config()), metadata
    )
    assert allowed == set()
    assert filtered.routing.agents["medium"] == []


@pytest.mark.django_db(transaction=True)
def test_launch_boundary_rechecks_admission_even_without_generic_tool(scoped):
    with pytest.raises(ValueError, match="repository alias"):
        scoped.plugin.start_agent(
            scoped.ctx,
            AgentSpec(
                "Fix",
                "path:/tmp/arbitrary",
                agent="omp",
                model="anthropic/claude-sonnet-5-5",
            ),
        )
    assert scoped.runner.calls == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("checkout_change", ["dirty", "ahead"])
def test_unsafe_created_checkout_is_retained_without_launch(scoped, checkout_change):
    scoped.runner.checkout_change = checkout_change
    with pytest.raises(ValueError, match="retained worktree id:wt-1") as error:
        start(scoped)
    assert "not clean at origin/main" in str(error.value)
    assert scoped.runner.paths[0].is_dir()
    assert scoped.runner.starts == 0
    assert scoped.scheduled == []
    assert not Worker.objects.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("configuration", ["disabled", "endpoint", "unlisted"])
def test_generic_start_honors_provider_model_allowlist(scoped, configuration):
    data = provider_config()
    provider = data["providers"]["claude"]
    if configuration == "disabled":
        provider["models"] = [
            {"name": "claude-sonnet-5-5", "config": {"enabled": False}}
        ]
    elif configuration == "endpoint":
        provider["config"] = {"base_url": "https://not-a-subscription.invalid"}
    else:
        provider["models"] = ["another-model"]
    scoped.bot.registry.providers = Providers.from_dict(data)
    with pytest.raises(ValueError, match="Scoped model"):
        start(scoped, model="anthropic/claude-sonnet-5-5", tier="medium")
    assert scoped.runner.calls == []


def test_filtered_routing_preserves_only_canonical_omp_candidates():
    data = provider_config()
    data["agents"]["medium"].insert(
        0,
        {
            "agent": "omp",
            "provider": "claude",
            "model": "unapproved/claude-sonnet-5-5",
        },
    )
    providers = Providers.from_dict(data)
    filtered, allowed = admitted_providers(
        providers,
        {
            "omp": True,
            "catalogs": CATALOGS,
            "subscriptions": SUBSCRIPTIONS,
        },
    )
    assert allowed == {"anthropic/claude-sonnet-5-5", "openai-codex/gpt-6-sol"}
    assert [
        (choice.agent, choice.model) for choice in filtered.routing.agents["medium"]
    ] == [
        ("claude", "claude-sonnet-5-5"),
        ("codex", "gpt-6-sol"),
    ]
    assert len(providers.routing.agents["medium"]) == 3


def test_scoped_medium_accepts_native_chat_from_real_omp_cache_shape():
    data = provider_config()
    data["agents"]["medium"] = [
        {"agent": "omp", "provider": "claude", "model": "claude-sonnet-5-5"},
        {"agent": "claude", "provider": "claude", "model": "claude-sonnet-5-5"},
    ]
    cache = {
        "id": "claude-sonnet-5-5",
        "api": "anthropic-messages",
        "input": ["text", "image"],
        "maxTokens": 16384,
        "cost": {"input": 1, "output": 2},
    }
    filtered, allowed = admitted_providers(
        Providers.from_dict(data),
        {
            "omp": True,
            "subscriptions": ["anthropic"],
            "catalogs": {"anthropic": [cache]},
        },
    )
    assert allowed == {"anthropic/claude-sonnet-5-5"}
    assert [(c.agent, c.model) for c in filtered.routing.agents["medium"]] == [
        ("claude", "claude-sonnet-5-5")
    ]


@pytest.mark.django_db(transaction=True)
def test_omp_only_scope_reports_existing_tier_filtered_by_policy(scoped):
    scoped.plugin.manager.agents = ("omp",)
    with pytest.raises(ValueError, match="tier exists but has no choices admitted"):
        start(scoped, tier="medium")
    with pytest.raises(ValueError, match="requires allowed agent claude"):
        start(scoped, model="anthropic/claude-sonnet-5-5", agent="omp")
    assert scoped.runner.calls == []


@pytest.mark.django_db
@pytest.mark.parametrize(
    "value",
    [
        "null",
        "{}",
        "[]",
        "{demo: demo}",
        "{'../demo': 'name:demo'}",
        "{demo: null}",
        "{demo: 'name:demo', other: 7}",
    ],
)
def test_invalid_scope_configuration_cannot_enable_unrestricted_mode(tmp_path, value):
    with pytest.raises(ValueError, match="scoped_repositories|Scoped repositories"):
        orca_bot(tmp_path, config=f"scoped_repositories: {value}")


def test_worker_only_xai_account_maps_to_canonical_subscription(execution_host):
    data = provider_config()
    data["providers"]["grok"] = {
        "type": "omp",
        "transport": "cli",
        "account": "xai-oauth",
    }
    data["agents"]["medium"] = [
        {
            "agent": "omp",
            "provider": "grok",
            "model": "xai-oauth/grok-test",
        }
    ]
    filtered, allowed = admitted_providers(Providers.from_dict(data), host_check())
    assert "xai-oauth/grok-test" in allowed
    [choice] = filtered.routing.agents["medium"]
    assert (choice.agent, choice.provider, choice.model) == (
        "omp",
        "grok",
        "xai-oauth/grok-test",
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("tier", ["", "medium"])
def test_private_model_cannot_be_hidden_by_tier_selection(scoped, tier):
    from django_ergo.conversation.inference_policy import PolicyError

    with pytest.raises(PolicyError, match="private model"):
        start(scoped, model="fireworks/accounts/private/model", tier=tier)
    assert scoped.runner.calls == []
    assert not Worker.objects.exists()


def test_missing_omp_never_admits_models():
    with pytest.raises(ValueError, match="requires omp"):
        admitted_providers(
            Providers.from_dict(provider_config()),
            {
                "omp": False,
                "catalogs": CATALOGS,
                "subscriptions": SUBSCRIPTIONS,
            },
        )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (
            "failed",
            {
                "dispatchId": "dispatch-1",
                "outcome": "failed",
                "disposition": "scope_blocked",
            },
            "scope_blocked",
        ),
        (
            "dispatched",
            {
                "dispatchId": "dispatch-1",
                "outcome": "failed",
                "disposition": "scope_blocked",
            },
            "scope_blocked",
        ),
        ("failed", {"dispatchId": "dispatch-1", "outcome": "failed"}, None),
        (
            "failed",
            {
                "dispatchId": "other",
                "taskId": "task-1",
                "outcome": "failed",
                "disposition": "scope_blocked",
            },
            None,
        ),
        (
            "completed",
            {
                "dispatchId": "dispatch-1",
                "outcome": "succeeded",
                "disposition": "scope_blocked",
            },
            None,
        ),
    ],
)
def test_scope_blocked_is_an_explicit_dispatch_scoped_assertion(
    scoped, status, payload, expected
):
    from django_ergo.bots import workers as w

    worker_id = start(scoped)["id"]
    scoped.runner.status = status
    scoped.runner.messages = [
        {
            "id": "done-1",
            "run_id": "run-1",
            "type": "worker_done",
            "body": "Report",
            "subject": "[scope_blocked] Blocked"
            if payload.get("disposition")
            else "Failure",
            "payload": json.dumps(
                {key: value for key, value in payload.items() if key != "disposition"}
            ),
            "sequence": 1,
        }
    ]
    assert w.run(worker_id, scoped.bot.registry) == "completed"
    result = Worker.objects.get(pk=worker_id).result
    assert result.get("disposition") == expected
    assert result["status"] == status
