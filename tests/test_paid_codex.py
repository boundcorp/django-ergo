"""Confinement refusal and supervised manager wiring; no isolation claim."""

import json
import sys
from types import SimpleNamespace

import pytest

from django_ergo.bots import pools
from django_ergo.bots.agents import AgentSpec
from django_ergo.conversation.models import PaidAgentLaunch
from django_ergo.plugins.local_shell import LocalShellAgents
from django_ergo.plugins.local_shell import LocalShellError
from django_ergo.plugins.paid_codex import PaidCodexPlugin
from tests.pool_funding import fund
from tests.test_local_shell import FAKE_CODEX
from tests.test_local_shell import wait_for_exit
from tests.test_model_pools import POOLS
from tests.test_model_pools import chat
from tests.test_model_pools import registry_of
from tests.test_model_pools import set_pref


class MockSandbox:
    """Test double ONLY: never asserts actual network isolation."""

    def prepare(self, **kwargs):
        return ["env"]


def plugin(tmp_path, **extra):
    instance = PaidCodexPlugin(
        None, {"root": str(tmp_path), "proxy_url": "https://proxy.invalid/v1", **extra}
    )
    instance.on_load()
    return instance


def test_default_refuses_before_process_or_token(tmp_path, monkeypatch):
    instance = plugin(tmp_path)
    calls = []
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: calls.append(kw))
    with pytest.raises(LocalShellError, match="no verified"):
        instance.start_agent(
            None,
            AgentSpec(
                agent="codex",
                workspace="job",
                brief="hi",
                confinement={"agent": "codex"},
            ),
        )
    assert not calls
    assert LocalShellAgents.metered_spend_bound is False
    assert instance.manager.agents == ("codex",)


@pytest.mark.parametrize(
    "filename", [".env", ".env.local", "auth.json", "config.toml", ".credentials.json"]
)
def test_visible_login_or_key_file_is_refused(tmp_path, filename):
    (tmp_path / filename).touch()
    with pytest.raises(LocalShellError, match="credentials or CLI login"):
        PaidCodexPlugin.check_visible_credentials(tmp_path)


def test_direct_base_url_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    with pytest.raises(LocalShellError, match="ambient"):
        PaidCodexPlugin.check_visible_credentials(tmp_path)


@pytest.mark.django_db(transaction=True)
def test_supervised_mock_codex_gets_capability_only(tmp_path, monkeypatch, settings):  # noqa: PLR0915 - supervised child/environment boundary
    from django.contrib.auth import get_user_model

    from django_ergo.bots.tools import ToolContext

    monkeypatch.setenv("TEST_POOL_OPENAI_KEY", "mock-pool-key")
    _, bots = registry_of(POOLS)
    bot = bots["devbox"]
    user = get_user_model().objects.create_user("lee")
    fund(bot.providers, user)
    session = chat(bot, user)
    set_pref(user.pk, bot.providers, active_pool="paid")
    decision = pools.select_agent(
        bot,
        session,
        model="openai/gpt-6.1-sol",
        agent="codex",
        manager_agents=("codex",),
        confinable=True,
        spend_bounded=True,
    )
    from django_ergo.conversation import weekly_budgets as weekly

    admin = get_user_model().objects.create_user("admin", is_superuser=True)
    weekly.assert_mapping(
        actor_id=admin.pk,
        owner_id=user.pk,
        identity=decision.transport_identity,
        account_label="mock account",
        assertion="Synthetic account assertion",
    )
    confinement = pools.confinement(bot, decision)
    fake = tmp_path / "fake-codex"
    fake.write_text(FAKE_CODEX.format(python=sys.executable))
    fake.chmod(0o755)
    instance = plugin(
        tmp_path / "jobs", executables={"codex": str(fake)}, timeout_minutes=0.1
    )
    ctx = ToolContext(bot=bot, session=session, user=user)
    settings.PAID_AGENT_SANDBOX_BACKEND = "tests.test_paid_codex.MockSandbox"
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    # Unit wiring test bypasses the CLOSED gate explicitly. It is no proof of isolation.
    monkeypatch.setattr(instance, "require_runtime", lambda: ["env"])
    monkeypatch.setattr(instance, "check_visible_credentials", lambda folder: None)
    monkeypatch.setenv("OPENAI_API_KEY", "mock-provider-key-never-for-child")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "mock-other-key")
    captured = []
    from django_ergo.conversation import paid_agent_proxy

    real_issue = paid_agent_proxy.issue

    def capture(**kwargs):
        result = real_issue(**kwargs)
        captured.append(result)
        return result

    monkeypatch.setattr("django_ergo.plugins.paid_codex.issue", capture)
    # Capture the actual supervisor env; the fake CLI never calls the network.
    import subprocess

    real_popen = subprocess.Popen
    environments = []

    def popen(*args, **kwargs):
        environments.append(kwargs["env"])
        return real_popen(*args, **kwargs)

    monkeypatch.setattr("django_ergo.plugins.local_shell.subprocess.Popen", popen)
    handle = instance.start_agent(
        ctx,
        AgentSpec(
            agent="codex",
            model=decision.model,
            workspace="job",
            brief="Say hello",
            confinement=confinement,
        ),
    )
    folder = instance.folder("job")
    wait_for_exit(folder, 1)
    env = environments[0]
    assert not any(
        k.endswith(("_API_KEY", "_AUTH_TOKEN", "_ACCESS_TOKEN")) for k in env
    )
    assert "OPENAI_BASE_URL" not in env and env["ERGO_PROXY_TOKEN"] == captured[0][1]
    assert env["HOME"] != str(tmp_path)
    from pathlib import Path

    assert Path(env["CODEX_HOME"]).is_dir()
    spec = json.loads((folder / ".ergo/run-1.json").read_text())
    assert 'model_provider="ergo_proxy"' in spec["argv"]
    assert captured[0][1] not in json.dumps(spec)
    assert not PaidAgentLaunch.objects.get(pk=handle["paid_launch"]).revoked_at
    # Watcher revocation does not depend on a successful supervisor HTTP callback.
    monkeypatch.setattr(
        "django_ergo.plugins.local_shell.LocalShellPlugin.check",
        lambda *a: SimpleNamespace(status="done"),
    )
    instance.check(ctx, handle)
    assert PaidAgentLaunch.objects.get(pk=handle["paid_launch"]).revoked_at
