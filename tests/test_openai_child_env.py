"""OpenAI-only launch/resume, override and bounded alias-collection controls."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from django.test import override_settings

from django_ergo.bots.definition import BotDefinition
from django_ergo.bots.providers import Provider
from django_ergo.bots.providers import parse_providers
from django_ergo.conversation.engines.claude_code import child_env as claude_env
from django_ergo.conversation.engines.codex_cli import child_env as codex_env
from django_ergo.openai_env import openai_credential_names
from django_ergo.openai_env import register_openai_alias
from django_ergo.plugins.local_shell import RUNNER
from django_ergo.plugins.local_shell import LocalShellPlugin
from django_ergo.protected_env import scrubbed_env


@pytest.mark.parametrize("factory", [scrubbed_env, claude_env, codex_env])
def test_openai_alias_override_grandchild_and_native_auth(factory, monkeypatch):
    register_openai_alias("ODD_OPENAI_ALIAS")
    blocked = {"OPENAI_API_KEY", "BOUNDCORP_ORG_OPENAI_KEY", "ODD_OPENAI_ALIAS"}
    native = {
        "HOME": "/fake",
        "CODEX_HOME": "/fake/codex",
        "CLAUDE_CONFIG_DIR": "/fake/claude",
        "CLAUDE_CODE_OAUTH_TOKEN": "fake-native",
    }
    for name, value in (dict.fromkeys(blocked, "fake") | native).items():
        monkeypatch.setenv(name, value)
    env = factory(dict.fromkeys(blocked, "fake-override") | native)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import subprocess,sys; subprocess.run([sys.executable,'-c','import os,json; print(json.dumps(dict(os.environ)))'],check=True)",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    child = json.loads(result.stdout)
    assert not blocked & child.keys()
    assert native.items() <= child.items()


@pytest.mark.parametrize("run", [1, 2], ids=["start", "resume"])
def test_local_shell_alias_cannot_return_via_pass_env_or_secrets(
    tmp_path, monkeypatch, run
):
    register_openai_alias("RESUME_ODD_ALIAS")
    blocked = {"RESUME_ODD_ALIAS", "BOUNDCORP_ORG_OPENAI_KEY"}
    for name in blocked:
        monkeypatch.setenv(name, "fake")
    plugin = LocalShellPlugin(
        None, {"pass_env": ["PATH", "HOME", *blocked], "secrets": list(blocked)}
    )
    plugin.on_load()
    meta = tmp_path / ".ergo"
    meta.mkdir()
    (meta / f"run-{run}.json").write_text(
        json.dumps(
            {
                "argv": [
                    sys.executable,
                    "-c",
                    "import os,json; print(json.dumps(sorted(os.environ)))",
                ],
                "timeout_seconds": 10,
            }
        )
    )
    subprocess.run(
        [sys.executable, str(RUNNER), str(tmp_path), str(run)],
        env=plugin.env(SimpleNamespace(secret=lambda _: "fake")),
        check=True,
    )
    assert not blocked & set(json.loads((meta / f"run-{run}.jsonl").read_text()))


def test_only_authoritative_openai_consumers_register_aliases(tmp_path):
    BotDefinition(name="openai", engine_type="openai", api_key_env="BOT_ALIAS")
    BotDefinition(
        name="google", engine_type="google", api_key_env="UNKNOWN_MEDIA_ALIAS"
    )
    Provider(name="x", type="openai", api_key_env="PROVIDER_ALIAS")
    parse_providers(
        "providers: {api: {type: openai, config: {api_key_env: CONFIG_ALIAS}, models: [{name: x, config: {api_key_env: MODEL_ALIAS}}]}}"
    )
    path = tmp_path / "unrelated.yaml"
    path.write_text("api_key_env: [broken")
    native = {
        "PUBLIC_VAR": "kept",
        "UNKNOWN_MEDIA_ALIAS": "fake",
        "CUSTOM_GCPJSON": "fake",
        "RYAN_MINIMAX_API_KEY": "fake",
    }
    with override_settings(
        DJANGO_ERGO={
            "VOICE_OPENAI_API_KEY_ENV": "VOICE_ALIAS",
            "CONVERSATION_ENGINE_TYPE": "openai",
            "CONVERSATION_ENGINE_CONFIG": {"api_key_env": "CHAT_ALIAS"},
            "EMBEDDING_PROVIDER_CONFIG": {"api_key_env": "EMBED_ALIAS"},
            "UNRELATED": {"api_key_env": "UNKNOWN_MEDIA_ALIAS"},
        }
    ):
        names = openai_credential_names()
        assert {
            "BOT_ALIAS",
            "PROVIDER_ALIAS",
            "CONFIG_ALIAS",
            "MODEL_ALIAS",
            "VOICE_ALIAS",
            "CHAT_ALIAS",
            "EMBED_ALIAS",
        } <= names
        assert not native.keys() & names
        assert scrubbed_env(native) == native
    # Reloads retain known names; unrelated files are never opened.
    assert "VOICE_ALIAS" in openai_credential_names()
    path.unlink()
    assert scrubbed_env(native) == native


@pytest.mark.parametrize("value", [[], {}, 123, "INVALID-NAME", "A" * 257])
def test_invalid_openai_selector_rejected_without_recursive_discovery(value):
    with pytest.raises(ValueError, match="environment variable name"):
        register_openai_alias(value)


def test_unrelated_embedding_and_chat_aliases_are_not_classified():
    with override_settings(
        DJANGO_ERGO={
            "CONVERSATION_ENGINE_TYPE": "google",
            "CONVERSATION_ENGINE_CONFIG": {"api_key_env": "OTHER_CHAT"},
            "EMBEDDING_PROVIDER": "unrelated.MediaProvider",
            "EMBEDDING_PROVIDER_CONFIG": {"api_key_env": "OTHER_MEDIA"},
        }
    ):
        assert not {"OTHER_CHAT", "OTHER_MEDIA"} & openai_credential_names()


async def test_final_cli_boundaries_recheck_aliases_after_env_was_built(monkeypatch):
    from django_ergo.conversation.engines import claude_code
    from django_ergo.conversation.engines import codex_cli

    env = {
        "LATE_ALIAS": "fake",
        "BOUNDCORP_ORG_OPENAI_KEY": "fake",
        "CLAUDE_CODE_OAUTH_TOKEN": "fake-native",
    }
    # The alias is registered after this prebuilt environment, as on a reload.
    register_openai_alias("LATE_ALIAS")
    seen = []

    async def capture(*args, **kwargs):
        seen.append(kwargs["env"])
        message = "captured fake launch"
        raise RuntimeError(message)

    monkeypatch.setattr(claude_code.asyncio, "create_subprocess_exec", capture)
    client = claude_code.ClaudeCodeClient(command="fake-cli")
    with pytest.raises(RuntimeError, match="captured fake launch"):
        await client._run([], [], env, "fake-model")
    with pytest.raises(RuntimeError, match="captured fake launch"):
        await codex_cli.model_catalog("fake-cli", [], env)
    session = codex_cli._Session(SimpleNamespace(command="fake-cli"), [], env)
    with pytest.raises(RuntimeError, match="captured fake launch"):
        await session.start()
    assert len(seen) == 3
    for child in seen:
        assert not {"LATE_ALIAS", "BOUNDCORP_ORG_OPENAI_KEY"} & child.keys()
        assert child["CLAUDE_CODE_OAUTH_TOKEN"] == "fake-native"


def test_bash_tool_boundary_strips_openai_only(tmp_path, monkeypatch):
    from django_ergo.plugins.bash import BashPlugin

    register_openai_alias("BASH_ALIAS")
    for name in (
        "BASH_ALIAS",
        "BOUNDCORP_ORG_OPENAI_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
    ):
        monkeypatch.setenv(name, "fake")
    plugin = BashPlugin(None, {"cwd": str(tmp_path)})
    plugin.on_load()
    seen = []

    def fake_run(*args, **kwargs):
        seen.append(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout="fake-local", stderr="")

    monkeypatch.setattr("django_ergo.plugins.bash.subprocess.run", fake_run)
    plugin.run("fake-command")
    assert len(seen) == 1
    assert (
        not {"BASH_ALIAS", "BOUNDCORP_ORG_OPENAI_KEY", "OPENAI_API_KEY"}
        & seen[0].keys()
    )
    assert seen[0]["GEMINI_API_KEY"] == "fake"
