"""Inherited-OpenAI bot aliases are withheld from children before any consumer runs.

Acceptance form of the PR #420 review P2 reproducers: fake keys only, no HTTP.
"""

from types import SimpleNamespace

import pytest
from django.test import override_settings

from django_ergo.bots.definition import BotDefinition
from django_ergo.conversation.engines.claude_code import child_env as claude_env
from django_ergo.conversation.engines.codex_cli import child_env as codex_env
from django_ergo.openai_env import openai_credential_names
from django_ergo.plugins.local_shell import LocalShellPlugin
from django_ergo.protected_env import scrubbed_env


def _engine(field, alias):
    if field == "top":
        return {"api_key_env": alias}
    return {"config": {"api_key_env": alias}}


@pytest.mark.parametrize("field", ["top", "config"])
@pytest.mark.parametrize("factory", [scrubbed_env, claude_env, codex_env])
def test_inherited_openai_bot_alias_withheld_before_first_consumer(
    field, factory, monkeypatch
):
    with override_settings(DJANGO_ERGO={"CONVERSATION_ENGINE_TYPE": "openai"}):
        definition = BotDefinition.from_dict(
            {"name": "inherited", "engine": _engine(field, "INHERITED_ALIAS")}
        )
        monkeypatch.setenv("INHERITED_ALIAS", "fake")
        assert definition.engine_type == ""
        assert "INHERITED_ALIAS" in openai_credential_names()
        assert "INHERITED_ALIAS" not in factory()
        assert "INHERITED_ALIAS" not in factory({"INHERITED_ALIAS": "fake"})


def test_library_default_is_openai_when_settings_unset(monkeypatch):
    BotDefinition.from_dict(
        {"name": "inherited", "engine": {"api_key_env": "INHERITED_DEFAULT"}}
    )
    assert "INHERITED_DEFAULT" in openai_credential_names()


@pytest.mark.parametrize("engine_type", ["google", "claude"])
def test_non_openai_default_or_explicit_engine_not_registered(engine_type, monkeypatch):
    monkeypatch.setenv("MEDIA_STYLE_ALIAS", "fake")
    with override_settings(DJANGO_ERGO={"CONVERSATION_ENGINE_TYPE": engine_type}):
        BotDefinition.from_dict(
            {"name": "other", "engine": {"api_key_env": "MEDIA_STYLE_ALIAS"}}
        )
        assert "MEDIA_STYLE_ALIAS" not in openai_credential_names()
        assert scrubbed_env()["MEDIA_STYLE_ALIAS"] == "fake"


@pytest.mark.parametrize("boundary", ["claude", "codex", "catalog"])
async def test_inherited_alias_withheld_at_final_cli_boundary(boundary, monkeypatch):
    from django_ergo.conversation.engines import claude_code
    from django_ergo.conversation.engines import codex_cli

    with override_settings(DJANGO_ERGO={"CONVERSATION_ENGINE_TYPE": "openai"}):
        BotDefinition.from_dict(
            {"name": "inherited", "engine": {"api_key_env": "INHERITED_FINAL"}}
        )
        env = {"INHERITED_FINAL": "fake", "BOUNDCORP_ORG_OPENAI_KEY": "fake"}
        seen = []

        async def capture(*args, **kwargs):
            seen.append(kwargs["env"])
            message = "fake launch captured"
            raise RuntimeError(message)

        monkeypatch.setattr(claude_code.asyncio, "create_subprocess_exec", capture)
        if boundary == "claude":
            launch = claude_code.ClaudeCodeClient(command="fake-cli")._run(
                [], [], env, "fake-model"
            )
        elif boundary == "catalog":
            launch = codex_cli.model_catalog("fake-cli", [], env)
        else:
            launch = codex_cli._Session(
                SimpleNamespace(command="fake-cli"), [], env
            ).start()
        with pytest.raises(RuntimeError, match="fake launch captured"):
            await launch
        assert len(seen) == 1
        assert "INHERITED_FINAL" not in seen[0]
        assert "BOUNDCORP_ORG_OPENAI_KEY" not in seen[0]


@pytest.mark.parametrize("run", ["start", "resume"])
def test_inherited_alias_withheld_from_local_shell_pass_env_and_secrets(
    run, monkeypatch
):
    with override_settings(DJANGO_ERGO={"CONVERSATION_ENGINE_TYPE": "openai"}):
        BotDefinition.from_dict(
            {"name": "inherited", "engine": {"api_key_env": "INHERITED_SHELL"}}
        )
        monkeypatch.setenv("INHERITED_SHELL", "fake")
        p = LocalShellPlugin(
            None,
            {"pass_env": ["PATH", "INHERITED_SHELL"], "secrets": ["INHERITED_SHELL"]},
        )
        p.on_load()
        env = p.env(SimpleNamespace(secret=lambda _: "fake-secret"))
        assert "INHERITED_SHELL" not in env


def test_malformed_unrelated_bot_yaml_does_not_break_env_building():
    # Nothing scans unloaded config; a bad bot only fails its own load.
    with pytest.raises(Exception):  # noqa: B017, PT011
        BotDefinition.from_dict({"name": "bad", "engine": "not-a-mapping"})
    assert "OPENAI_API_KEY" not in scrubbed_env({"OPENAI_API_KEY": "fake"})
