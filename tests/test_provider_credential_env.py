"""Fake-only environment checks, including a real child and grandchild."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from django.test import override_settings

from django_ergo.conversation.engines.claude_code import child_env as claude_env
from django_ergo.conversation.engines.codex_cli import child_env as codex_env
from django_ergo.plugins.local_shell import LocalShellPlugin
from django_ergo.protected_env import scrubbed_env
from django_ergo.provider_credentials import SDK_CREDENTIALS
from django_ergo.provider_credentials import register_credential_config
from django_ergo.provider_credentials import register_credential_source


@pytest.mark.parametrize("factory", [scrubbed_env, claude_env, codex_env])
def test_all_provider_names_removed_without_subscription_changes(monkeypatch, factory):
    register_credential_config(
        {"providers": {"disabled": {"api_key_env": "ODD_ALIAS"}}}
    )
    for name in SDK_CREDENTIALS | {"ODD_ALIAS"}:
        monkeypatch.setenv(name, "fake-provider-secret")
    monkeypatch.setenv("HOME", "/fake/subscription-home")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-subscription")
    monkeypatch.setenv("ANTHROPIC_OAUTH_TOKEN", "fake-native-oauth")
    monkeypatch.setenv("CODEX_HOME", "/fake/codex-home")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/fake/claude-home")
    env = factory()
    assert not (SDK_CREDENTIALS | {"ODD_ALIAS"}) & env.keys()
    assert env["HOME"] == "/fake/subscription-home"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "fake-subscription"
    if factory is claude_env:
        # Keep Claude Code's existing prefix policy; its native OAuth variable
        # is CLAUDE_CODE_OAUTH_TOKEN, unlike omp's ANTHROPIC_OAUTH_TOKEN.
        assert "ANTHROPIC_OAUTH_TOKEN" not in env
    else:
        assert env["ANTHROPIC_OAUTH_TOKEN"] == "fake-native-oauth"
    assert env["CODEX_HOME"] == "/fake/codex-home"
    assert env["CLAUDE_CONFIG_DIR"] == "/fake/claude-home"


@pytest.mark.parametrize("factory", [claude_env, codex_env])
def test_overrides_cannot_reintroduce_provider_alias(factory):
    register_credential_config({"api_key_env": "ODD_ALIAS"})
    assert "ODD_ALIAS" not in factory({"ODD_ALIAS": "fake"})
    assert "OPENAI_API_KEY" not in factory({"OPENAI_API_KEY": "fake"})


def test_sources_reread_before_launch_and_old_names_remain_blocked(tmp_path):
    path = tmp_path / "providers.yaml"
    path.write_text("providers: {one: {api_key_env: OLD_ALIAS}}")
    register_credential_source(path)
    assert scrubbed_env({"OLD_ALIAS": "fake"}) == {}
    path.write_text("providers: {one: {api_key_env: NEW_ALIAS}}")
    assert scrubbed_env({"NEW_ALIAS": "fake", "OLD_ALIAS": "fake"}) == {}
    path.unlink()
    assert scrubbed_env({"NEW_ALIAS": "fake", "OLD_ALIAS": "fake"}) == {}
    path.write_text("providers: {one: {api_key_env: RECREATED_ALIAS}}")
    assert scrubbed_env({"RECREATED_ALIAS": "fake", "OLD_ALIAS": "fake"}) == {}


@override_settings(
    DJANGO_ERGO={
        "VOICE_OPENAI_API_KEY_ENV": "VOICE_ALIAS",
        "EMBEDDING_PROVIDER_CONFIG": {"api_key_env": "EMBED_ALIAS"},
    }
)
def test_settings_aliases_are_removed_before_client_initialization():
    assert scrubbed_env({"VOICE_ALIAS": "fake", "EMBED_ALIAS": "fake"}) == {}


def test_local_shell_pass_and_secret_lists_cannot_bypass_scrub(monkeypatch):
    register_credential_config({"api_key_env": "ODD_ALIAS"})
    plugin = LocalShellPlugin(
        None,
        {"pass_env": ["HOME", "ODD_ALIAS"], "secrets": ["ODD_ALIAS", "TOOL_TOKEN"]},
    )
    plugin.on_load()
    monkeypatch.setenv("ODD_ALIAS", "fake")
    monkeypatch.setenv("HOME", "/subscription-home")
    env = plugin.env(SimpleNamespace(secret=lambda name: "fake"))
    assert env == {"HOME": "/subscription-home", "TOOL_TOKEN": "fake"}


@pytest.mark.parametrize("factory", [scrubbed_env, claude_env, codex_env])
def test_child_and_grandchild_inherit_no_provider_credentials(monkeypatch, factory):
    register_credential_config({"api_key_env": "ODD_ALIAS"})
    for name in SDK_CREDENTIALS | {"ODD_ALIAS"}:
        monkeypatch.setenv(name, "fake-secret")
    probe = "import json,os; print(json.dumps(sorted(os.environ)))"
    script = "import subprocess,sys; subprocess.run([sys.executable, '-c', sys.argv[1]], check=True)"
    result = subprocess.run(
        [sys.executable, "-c", script, probe],
        env=factory(),
        capture_output=True,
        text=True,
        check=True,
    )
    assert not (SDK_CREDENTIALS | {"ODD_ALIAS"}) & set(json.loads(result.stdout))


@pytest.mark.parametrize("launcher", ["bash", "orca", "search"])
def test_plugin_launchers_pass_scrubbed_env(monkeypatch, tmp_path, launcher):
    from django_ergo.plugins.bash import BashPlugin
    from django_ergo.plugins.orca import OrcaPlugin
    from django_ergo.plugins.web_search import WebSearchPlugin

    register_credential_config({"api_key_env": "ODD_ALIAS"})
    monkeypatch.setenv("ODD_ALIAS", "fake")
    monkeypatch.setenv("BOUNDCORP_ORG_OPENAI_KEY", "fake")
    captured = []

    def run(*args, **kwargs):
        captured.append(kwargs["env"])
        return subprocess.CompletedProcess(args[0], 0, '{"result":"offline"}', "")

    monkeypatch.setattr(subprocess, "run", run)
    if launcher == "bash":
        plugin = BashPlugin(None, {"cwd": str(tmp_path)})
        plugin.on_load()
        plugin.run("offline")
    elif launcher == "orca":
        plugin = OrcaPlugin(None, {})
        plugin.on_load()
        plugin.run(["status"])
    else:
        plugin = WebSearchPlugin(None, {"backend": "claude_code"})
        plugin.on_load()
        plugin._search_claude_code("offline", 1)
    assert captured
    assert all(not (SDK_CREDENTIALS | {"ODD_ALIAS"}) & env.keys() for env in captured)


@pytest.mark.parametrize("number", [1, 2])
def test_local_runner_and_resumed_descendants_are_keyless(
    monkeypatch, tmp_path, number
):
    from django_ergo.plugins.local_shell import RUNNER

    register_credential_config({"api_key_env": "ODD_ALIAS"})
    monkeypatch.setenv("ODD_ALIAS", "fake")
    plugin = LocalShellPlugin(
        None, {"pass_env": ["PATH", "HOME", "ODD_ALIAS"], "secrets": ["ODD_ALIAS"]}
    )
    plugin.on_load()
    meta = tmp_path / ".ergo"
    meta.mkdir()
    probe = "import os,json; print(json.dumps(sorted(os.environ)))"
    script = "import subprocess,sys; subprocess.run([sys.executable,'-c',sys.argv[1]],check=True)"
    (meta / f"run-{number}.json").write_text(
        json.dumps(
            {"argv": [sys.executable, "-c", script, probe], "timeout_seconds": 10}
        )
    )
    subprocess.run(
        [sys.executable, str(RUNNER), str(tmp_path), str(number)],
        env=plugin.env(SimpleNamespace(secret=lambda name: "fake")),
        check=True,
    )
    names = json.loads((meta / f"run-{number}.jsonl").read_text())
    assert not (SDK_CREDENTIALS | {"ODD_ALIAS"}) & set(names)


def test_direct_and_unselected_provider_and_bot_aliases_are_registered():
    from django_ergo.bots.definition import BotDefinition
    from django_ergo.bots.providers import Provider
    from django_ergo.bots.providers import Providers

    Provider(name="disabled", type="google", enabled=False, api_key_env="DIRECT_ALIAS")
    Providers.from_dict(
        {"providers": {"other": {"type": "openai", "api_key_env": "UNSELECTED_ALIAS"}}}
    )
    BotDefinition(name="unselected", api_key_env="BOT_ALIAS")
    assert (
        scrubbed_env(
            dict.fromkeys(("DIRECT_ALIAS", "UNSELECTED_ALIAS", "BOT_ALIAS"), "fake")
        )
        == {}
    )


def test_config_source_symlink_replacement_is_rechecked(tmp_path):
    old, new, link = (tmp_path / name for name in ("old.yaml", "new.yaml", "bot.yaml"))
    old.write_text("engine: {api_key_env: OLD_ALIAS}")
    new.write_text("engine: {api_key_env: NEW_ALIAS}")
    link.symlink_to(old)
    register_credential_source(link)
    assert scrubbed_env({"OLD_ALIAS": "fake"}) == {}
    link.unlink()
    link.symlink_to(new)
    assert scrubbed_env({"OLD_ALIAS": "fake", "NEW_ALIAS": "fake"}) == {}


def test_registry_registers_all_bot_files_before_loading_any_bot(tmp_path, monkeypatch):
    from django_ergo.bots.registry import BotRegistry

    folders = [tmp_path / name for name in ("first", "second")]
    for folder, name in zip(folders, ("FIRST_ALIAS", "SECOND_ALIAS"), strict=True):
        folder.mkdir()
        (folder / "bot.yaml").write_text(f"engine: {{api_key_env: {name}}}")
    seen = []

    def load(self, path, **kwargs):
        seen.append(scrubbed_env({"FIRST_ALIAS": "fake", "SECOND_ALIAS": "fake"}))

    monkeypatch.setattr(BotRegistry, "load", load)
    BotRegistry.from_paths(iter(folders))
    assert seen == [{}, {}]


def test_direct_api_engine_configuration_registers_other_provider_aliases():
    from django_ergo.conversation.engines.claude_api import ClaudeAPIEngine
    from django_ergo.conversation.engines.google_api import GoogleAPIEngine

    ClaudeAPIEngine({"api_key_env": "CLAUDE_CUSTOM"})
    GoogleAPIEngine({"api_key_env": "GEMINI_CUSTOM"})
    assert scrubbed_env({"CLAUDE_CUSTOM": "fake", "GEMINI_CUSTOM": "fake"}) == {}


def test_invalid_existing_credential_source_blocks_launch(tmp_path):
    import yaml

    path = tmp_path / "bot.yaml"
    path.write_text("engine: [")
    register_credential_source(path)
    with pytest.raises(yaml.YAMLError):
        scrubbed_env({"ODD_ALIAS": "fake"})
