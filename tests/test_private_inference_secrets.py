"""Protected variables (FIREWORKS_* and configured prefixes) never reach tools
or subprocesses."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model

from django_ergo.bots.tools import ToolContext
from django_ergo.conversation.engines import claude_code
from django_ergo.conversation.engines import codex_cli
from django_ergo.conversation.inference_policy import PolicyError
from django_ergo.plugins.bash import BashPlugin
from django_ergo.plugins.local_shell import LocalShellPlugin
from django_ergo.plugins.orca import OrcaPlugin
from django_ergo.plugins.telegram import TelegramPlugin

SENTINEL = "fw-test-sentinel-123"


@pytest.fixture
def protected(monkeypatch):
    monkeypatch.setenv("FIREWORKS_API_KEY", SENTINEL)
    monkeypatch.setenv("FIREWORKS_OTHER", SENTINEL)
    monkeypatch.setenv("ERGO_PUBLIC_VAR", "kept")


@pytest.fixture
def house_prefix(settings, monkeypatch):
    settings.DJANGO_ERGO = {
        **getattr(settings, "DJANGO_ERGO", {}),
        "PROTECTED_ENV_PREFIXES": ["house_"],
    }
    monkeypatch.setenv("HOUSE_TOKEN", SENTINEL)


def assert_scrubbed(env: dict) -> None:
    assert not [k for k in env if k.upper().startswith(("FIREWORKS_", "HOUSE_"))]
    assert SENTINEL not in env.values()


def capture_run(monkeypatch, target: str, stdout="", returncode=0) -> list[dict]:
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(target, fake_run)
    return calls


def test_bash_commands_get_a_scrubbed_environment(
    tmp_path, monkeypatch, protected, house_prefix
):
    calls = capture_run(monkeypatch, "django_ergo.plugins.bash.subprocess.run")
    plugin = BashPlugin(None, {"cwd": str(tmp_path)})
    plugin.on_load()
    plugin.run("env")
    [kwargs] = calls
    assert_scrubbed(kwargs["env"])
    assert kwargs["env"]["ERGO_PUBLIC_VAR"] == "kept"


def test_local_shell_never_passes_protected_variables(protected, house_prefix):
    plugin = LocalShellPlugin(
        None,
        {"pass_env": ["PATH", "FIREWORKS_API_KEY", "FIREWORKS_OTHER", "HOUSE_TOKEN"]},
    )
    plugin.on_load()
    env = plugin.env(ToolContext())
    assert_scrubbed(env)
    assert "PATH" in env

    for name in ("FIREWORKS_API_KEY", "fireworks_other", "HOUSE_TOKEN"):
        refused = LocalShellPlugin(None, {"secrets": [name]})
        with pytest.raises(ValueError, match=f"won't pass {name}: reserved") as exc:
            refused.on_load()
        assert SENTINEL not in str(exc.value)


@pytest.mark.parametrize("engine", [codex_cli, claude_code])
def test_agent_cli_child_env_strips_protected_variables(
    engine, protected, house_prefix
):
    env = engine.child_env({"ERGO_OVERRIDE": "1"})
    assert_scrubbed(env)
    assert env["ERGO_PUBLIC_VAR"] == "kept"
    assert env["ERGO_OVERRIDE"] == "1"
    for name in ("FIREWORKS_API_KEY", "HOUSE_TOKEN"):
        with pytest.raises(PolicyError, match="reserved for private inference") as exc:
            engine.child_env({name: SENTINEL})
        assert SENTINEL not in str(exc.value)


def test_orca_cli_runs_get_a_scrubbed_environment(monkeypatch, protected, house_prefix):
    calls = []

    class RanError(Exception):
        pass

    def fake_run(argv, **kwargs):
        calls.append(kwargs)
        raise RanError

    monkeypatch.setattr("django_ergo.plugins.orca.subprocess.run", fake_run)
    plugin = OrcaPlugin(None, {"executable": "orca-test"})
    plugin.on_load()
    ctx = SimpleNamespace(session=object())
    for call in (
        lambda: plugin.run(["status"]),
        lambda: plugin.worktree_path("wt"),
        lambda: plugin.fetch("/w", "a.txt"),
        lambda: plugin.screenshot(ctx),
        lambda: plugin.push("/w", "", "a.txt", b"x"),
        lambda: plugin.cli_json(["status"]),
        lambda: plugin.run_scanner([]),
    ):
        with pytest.raises(RanError):
            call()
    assert len(calls) == 7
    for kwargs in calls:
        assert_scrubbed(kwargs["env"])


def test_tool_secret_refuses_protected_names(protected, house_prefix):
    user = get_user_model()(username="aud")
    for ctx in (ToolContext(), ToolContext(user=user)):
        for name in ("FIREWORKS_API_KEY", "fireworks_other", "HOUSE_TOKEN"):
            with pytest.raises(PolicyError) as exc:
                ctx.secret(name)
            assert SENTINEL not in str(exc.value)
    with pytest.raises(PolicyError) as exc:
        ToolContext().secret("FIREWORKS_API_KEY")
    assert str(exc.value) == (
        "FIREWORKS_API_KEY is reserved for private inference and can't be read by tools"
    )
    # A name whose per-user form is protected: FIREWORKS + __AUD.
    with pytest.raises(PolicyError, match="FIREWORKS__AUD is reserved"):
        ToolContext(user=user).secret("FIREWORKS")
    assert ToolContext(user=user).secret("ERGO_PUBLIC_VAR") == "kept"


def test_telegram_env_names_cannot_be_protected(protected):
    plugin = TelegramPlugin(
        None, {"token_env": "FIREWORKS_API_KEY", "secret_env": "FIREWORKS_OTHER"}
    )
    plugin.on_load()
    with pytest.raises(PolicyError, match="reserved for private inference") as exc:
        _ = plugin.api
    assert SENTINEL not in str(exc.value)
    plugin.api = SimpleNamespace(token="t")
    with pytest.raises(PolicyError, match="FIREWORKS_OTHER is reserved"):
        _ = plugin.webhook_secret


def test_browser_launch_and_ssh_get_a_scrubbed_environment(
    monkeypatch, protected, house_prefix
):
    from django_ergo.plugins.browser import BrowserPlugin

    calls = capture_run(
        monkeypatch, "django_ergo.plugins.browser.subprocess.run", returncode=1
    )
    plugin = BrowserPlugin(None, {"launch_command": "chrome --headless"})
    plugin.on_load()
    with pytest.raises(RuntimeError, match="launch_command failed"):
        plugin._launch()  # bash -lc
    plugin._ssh("host", "true")
    assert len(calls) == 2
    for kwargs in calls:
        assert_scrubbed(kwargs["env"])


def test_bot_management_commands_get_a_scrubbed_environment(
    tmp_path, monkeypatch, protected, house_prefix
):
    from django_ergo.plugins.bot_management import BotManagementPlugin

    calls = capture_run(
        monkeypatch,
        "django_ergo.plugins.bot_management.subprocess.run",
        stdout='{"ok": true, "preview": "hi"}',
    )
    repo = tmp_path.resolve()
    (repo / "bot").mkdir()
    (repo / "bot" / "bot.yaml").write_text("name: shop\ntables: {}\n")
    (repo / "bot" / "home.jhtml").write_text("<p>hi</p>")
    plugin = BotManagementPlugin(
        None, {"mode": "merge_main", "run": {"commands": {"check": ["true"]}}}
    )
    plugin.on_load()
    plugin._repo = repo
    plugin.git("status")
    plugin.run_command("check")
    plugin.preview("bot/home.jhtml")  # makemigrations, then the preview
    plugin.live_read("bot/bot.yaml")
    assert len(calls) == 5
    for kwargs in calls:
        assert_scrubbed(kwargs["env"])
    run_env = calls[1]["env"]
    assert run_env["CI"] == "1" and "ERGO_PUBLIC_VAR" not in run_env
    assert calls[2]["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert calls[2]["env"]["ERGO_PUBLIC_VAR"] == "kept"


def test_other_subprocess_callers_get_a_scrubbed_environment(
    tmp_path, monkeypatch, protected, house_prefix
):
    from django_ergo.bots import overview
    from django_ergo.bots import usage_sync
    from django_ergo.bots.folder_kb import FolderKB
    from django_ergo.conversation import links
    from django_ergo.filesystem_kb import FilesystemKBError
    from django_ergo.filesystem_kb import RepositoryCorpus
    from django_ergo.git_snapshot import git_environment
    from django_ergo.knowledge.git import GitCorpus
    from django_ergo.knowledge.schema import CorpusError
    from django_ergo.plugins import agent_usage_scan
    from django_ergo.plugins.kubectl import KubectlPlugin

    calls = capture_run(monkeypatch, "subprocess.run", returncode=1)
    monkeypatch.setattr("django_ergo.bots.overview.shutil.which", lambda _: "gh")
    KubectlPlugin._run_argv(SimpleNamespace(timeout=5), ["kubectl", "version"])
    with pytest.raises(usage_sync.UsageSyncError):
        usage_sync.fetch_report("omp usage --json", timeout=5)
    assert links.refresh_pull_request(SimpleNamespace(url="https://x/pull/1")) is False
    agent_usage_scan._git(str(tmp_path), "status")
    overview.gh_open_prs(["secret-hygiene/test-repo"])
    FolderKB(tmp_path)._commit(tmp_path / "a.md", "msg", "me")
    with pytest.raises(FilesystemKBError):
        RepositoryCorpus._run(tmp_path, "status")
    corpus = GitCorpus(tmp_path, ref="HEAD", collection_id="c", scope="s")
    with pytest.raises(CorpusError):
        corpus._git("status")
    assert len(calls) == 8
    for kwargs in calls:
        assert_scrubbed(kwargs["env"])
    assert_scrubbed(git_environment())
