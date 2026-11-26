from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from django_ergo.bots.definition import BotDefinition
from django_ergo.bots.providers import Providers
from django_ergo.bots.providers import ProvidersError
from django_ergo.bots.providers import load_providers
from django_ergo.bots.registry import BotRegistry
from django_ergo.bots.runtime import Bot

PROVIDERS = """
default: openai/gpt-6-luna
providers:
  openai:
    type: openai
    api_key_env: TEST_OPENAI_KEY
    config: {reasoning_effort: medium}
    models: [gpt-6-luna, gpt-6-sol]
  anthropic:
    type: claude
    api_key_env: TEST_ANTHROPIC_KEY
    models:
      - {name: claude-opus-5-5, label: Opus 5.5, config: {max_tokens: 16000}}
"""


def providers(tmp_path) -> Providers:
    (tmp_path / "providers.yaml").write_text(PROVIDERS)
    return load_providers([tmp_path / "missing", tmp_path])


def bot_with(found: Providers, engine: dict | None = None) -> Bot:
    bot = Bot(BotDefinition.from_dict({"name": "kitchen", "engine": engine or {}}))
    registry = BotRegistry()
    registry.providers = found
    registry.add(bot)
    return bot


def worker_provider_data():
    data = yaml.safe_load(PROVIDERS)
    data["providers"]["xai-oauth"] = {
        "type": "omp",
        "transport": "cli",
        "account": "xai-oauth",
    }
    data["agents"] = {
        "small": [
            {
                "agent": "omp",
                "provider": "xai-oauth",
                "model": "xai-oauth/grok-4",
            }
        ]
    }
    return data


@pytest.mark.parametrize(
    "change",
    [
        {"transport": "api"},
        {"transport": None},
        {"models": ["grok-4"]},
        {"api_key_env": "TEST_XAI_KEY"},
        {"config": {"command": "omp"}},
        {"config": {"api_key": "not-a-credential"}},
        {"account": "xai"},
        {"account": ""},
        {"account": None},
    ],
    ids=[
        "api-transport",
        "omitted-transport",
        "chat-models",
        "credential-env",
        "cli-config",
        "credential-config",
        "unsupported-account",
        "empty-account",
        "omitted-account",
    ],
)
def test_worker_provider_rejects_unsupported_configuration(change):
    data = worker_provider_data()
    spec = data["providers"]["xai-oauth"]
    for key, value in change.items():
        if value is None:
            spec.pop(key)
        else:
            spec[key] = value
    with pytest.raises(ProvidersError):
        Providers.from_dict(data)


@pytest.mark.parametrize("name", ["anthropic", "openai-codex"])
def test_worker_alias_cannot_overwrite_another_accounts_canonical_usage(name):
    with pytest.raises(ProvidersError):
        Providers.from_dict(
            {
                "providers": {
                    name: {
                        "type": "omp",
                        "transport": "cli",
                        "account": "xai-oauth",
                    }
                }
            }
        )


@pytest.mark.parametrize("kind", ["openai", "claude"])
@pytest.mark.parametrize("account", ["xai-oauth", ""])
@pytest.mark.parametrize("transport", ["api", "cli"])
def test_account_is_not_accepted_on_chat_providers(kind, account, transport):
    data = worker_provider_data()
    data["providers"]["ordinary"] = {
        "type": kind,
        "transport": transport,
        "account": account,
        "models": ["ordinary-model"],
    }
    with pytest.raises(ProvidersError):
        Providers.from_dict(data)


@pytest.mark.parametrize(
    "change",
    [
        {"agent": "claude"},
        {"agent": "codex"},
        {"model": "grok-4"},
        {"model": "xai/grok-4"},
        {"model": "anthropic/grok-4"},
        {"model": "xai-oauth/"},
        {"model": "xai-oauth/grok-4/extra"},
        {"model": ""},
    ],
)
def test_worker_provider_requires_omp_and_an_exact_account_model(change):
    data = worker_provider_data()
    data["agents"]["small"][0].update(change)
    with pytest.raises(ProvidersError):
        Providers.from_dict(data)


@pytest.mark.parametrize("reference", ["default", "auto-default", "tier"])
def test_worker_models_cannot_be_default_or_chat_tier_candidates(reference):
    data = worker_provider_data()
    if reference == "default":
        data["default"] = "xai-oauth/grok-4"
    elif reference == "auto-default":
        data["default"] = "auto/small"
    else:
        data["tiers"] = {"small": ["xai-oauth/grok-4"]}
    with pytest.raises(ProvidersError):
        Providers.from_dict(data)


def test_worker_provider_is_never_a_chat_engine_or_model_picker_option(monkeypatch):
    monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test")
    found = Providers.from_dict(worker_provider_data())
    worker = found.providers["xai-oauth"]
    assert worker.worker_only
    assert worker.account == "xai-oauth"
    assert not found.providers["openai"].worker_only
    assert found.providers["openai"].account == ""
    assert [m.id for m in found.models()] == [
        "openai/gpt-6-luna",
        "openai/gpt-6-sol",
        "anthropic/claude-opus-5-5",
    ]
    assert found.find("xai-oauth/grok-4") is None
    assert not found.knows("xai-oauth/grok-4")
    # Configured worker tiers do not make an ordinary auto chat tier usable.
    assert not found.knows("auto/small")
    with pytest.raises(ProvidersError):
        found.engine("xai-oauth/grok-4")

    bot = bot_with(found)
    assert bot.session_model_ref("xai-oauth/grok-4") == ""
    chat = SimpleNamespace(engine_type="openai", model="xai-oauth/grok-4")
    spec = bot.engine_spec(chat)
    assert (spec.engine_type, spec.config["model"]) == ("openai", "gpt-6-luna")
    assert spec.config["provider"] == "openai"


def test_worker_provider_requires_an_executable_omp_on_path(monkeypatch, tmp_path):
    worker = Providers.from_dict(worker_provider_data()).providers["xai-oauth"]
    monkeypatch.setenv("PATH", str(tmp_path))
    assert not worker.available
    executable = tmp_path / "omp"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o644)
    assert not worker.available
    executable.chmod(0o755)
    assert worker.available


def test_providers_yaml_lists_models_and_builds_their_engines(tmp_path, monkeypatch):
    found = providers(tmp_path)
    assert [m.id for m in found.models()] == [
        "openai/gpt-6-luna",
        "openai/gpt-6-sol",
        "anthropic/claude-opus-5-5",
    ]
    assert found.engine("anthropic/claude-opus-5-5") == (
        "claude",
        {"max_tokens": 16000, "model": "claude-opus-5-5"},
        "TEST_ANTHROPIC_KEY",
    )
    monkeypatch.delenv("TEST_ANTHROPIC_KEY", raising=False)
    monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test")
    assert found.providers["openai"].available
    assert not found.providers["anthropic"].available
    with pytest.raises(ProvidersError, match="Unknown model"):
        found.engine("openai/gpt-2")
    with pytest.raises(ProvidersError, match="isn't one of the listed models"):
        Providers.from_dict({"default": "x/y"})
    with pytest.raises(ProvidersError, match="type must be one of"):
        Providers.from_dict({"providers": {"gemini": {"models": ["g"]}}})
    assert not load_providers([tmp_path / "nowhere"])


def test_bots_and_chats_pick_models_from_providers(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test")
    found = providers(tmp_path)

    # A bot with no engine uses the default; one can name a provider/model.
    spec = bot_with(found).engine_spec()
    assert (spec.engine_type, spec.config["model"], spec.config["api_key"]) == (
        "openai",
        "gpt-6-luna",
        "sk-test",
    )
    sol = bot_with(found, {"config": {"model": "openai/gpt-6-sol", "max_tokens": 900}})
    spec = sol.engine_spec()
    assert spec.config == {
        "reasoning_effort": "medium",
        "model": "gpt-6-sol",
        "max_tokens": 900,
        "provider": "openai",
        "api_key": "sk-test",
        "api_key_env": "TEST_OPENAI_KEY",
    }

    # A chat's pick wins, on any engine.
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "sk-ant")
    bot = bot_with(found)
    chat = SimpleNamespace(engine_type="openai", model="openai/gpt-6-sol")
    assert bot.engine_spec(chat).config["model"] == "gpt-6-sol"
    chat.model = "anthropic/claude-opus-5-5"
    spec = bot.engine_spec(chat)
    assert (spec.engine_type, spec.config["model"]) == ("claude", "claude-opus-5-5")
    chat.model = "openai/gone"  # no longer listed: ignored
    assert bot.engine_spec(chat).config["model"] == "gpt-6-luna"

    # The old engine form still works without providers.
    plain = bot_with(Providers(), {"type": "openai", "config": {"model": "gpt-6-luna"}})
    assert plain.engine_spec().config == {"model": "gpt-6-luna"}

    monkeypatch.delenv("TEST_OPENAI_KEY")
    with pytest.raises(RuntimeError, match="TEST_OPENAI_KEY"):
        bot.engine_spec()


def test_chats_follow_the_bot_to_another_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test")
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "sk-ant")
    found = Providers.from_dict(
        {**yaml.safe_load(PROVIDERS), "default": "anthropic/claude-opus-5-5"}
    )
    bot = bot_with(found)
    assert bot.engine_spec().engine_type == "claude"  # new chats

    # Messages are engine-neutral: a chat last run on OpenAI follows the default.
    old = SimpleNamespace(engine_type="openai", model="")
    spec = bot.engine_spec(old)
    assert (spec.engine_type, spec.config["model"]) == ("claude", "claude-opus-5-5")
    picked = SimpleNamespace(engine_type="claude", model="openai/gpt-6-sol")
    assert bot.engine_spec(picked).config["model"] == "gpt-6-sol"


def test_model_context_window_suffix_and_override(tmp_path):
    providers = Providers.from_dict(
        {
            "providers": {
                "subscription": {
                    "type": "claude",
                    "transport": "cli",
                    "models": [
                        "claude-opus-5-5[1m]",
                        {"name": "small", "context_window": 64000},
                    ],
                }
            }
        }
    )
    first = bot_with(
        providers, {"config": {"model": "subscription/claude-opus-5-5[1m]"}}
    )
    engine = first.make_engine()
    assert engine.context_window == 1000000
    assert engine.model == "claude-opus-5-5[1m]"
    engine = first.make_engine(model="subscription/small")
    assert engine.context_window == 64000
    assert engine.model == "small"


@pytest.mark.django_db
def test_explicit_reroute_is_used_once_before_pace_routing_resumes(
    django_user_model, monkeypatch
):
    from django_ergo.conversation.models import ConversationSession
    from django_ergo.conversation.models import RoutingSwitch

    monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test")
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "sk-ant")
    config = yaml.safe_load(PROVIDERS)
    config["default"] = "auto/medium"
    config["tiers"] = {"medium": ["openai/gpt-6-sol", "anthropic/claude-opus-5-5"]}
    bot = bot_with(Providers.from_dict(config))
    monkeypatch.setattr("django_ergo.bots.runtime.ensure_compiled", lambda *args: None)
    monkeypatch.setattr(
        "django_ergo.bots.runtime.route_model",
        lambda providers, tier, current: "openai/gpt-6-sol",
    )
    user = django_user_model.objects.create(username="retry")
    chat = ConversationSession.objects.create(
        user=user, bot_name="kitchen", model="auto/medium", engine_type="openai"
    )
    assert bot.route(chat) == "openai/gpt-6-sol"
    bot.reroute(chat, "anthropic/claude-opus-5-5", "retry after refusal")

    # The resume endpoint queues a new turn, which loads the session again.
    chat.refresh_from_db()
    assert bot.route(chat) == "anthropic/claude-opus-5-5"
    assert bot.engine_spec(chat).config["model"] == "claude-opus-5-5"
    chat.refresh_from_db()
    assert "retry_route" not in chat.metadata
    assert bot.route(chat) == "openai/gpt-6-sol"
    assert list(
        RoutingSwitch.objects.order_by("created_at").values_list("to_model", flat=True)
    ) == ["anthropic/claude-opus-5-5", "openai/gpt-6-sol"]


@pytest.mark.django_db
def test_retry_route_is_discarded_when_model_changes_or_becomes_unavailable(
    django_user_model, monkeypatch
):
    from django_ergo.conversation.models import ConversationSession

    monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test")
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "sk-ant")
    config = yaml.safe_load(PROVIDERS)
    config["default"] = "auto/medium"
    config["tiers"] = {"medium": ["openai/gpt-6-sol", "anthropic/claude-opus-5-5"]}
    bot = bot_with(Providers.from_dict(config))
    monkeypatch.setattr("django_ergo.bots.runtime.ensure_compiled", lambda *args: None)
    monkeypatch.setattr(
        "django_ergo.bots.runtime.route_model",
        lambda providers, tier, current: "openai/gpt-6-sol",
    )
    user = django_user_model.objects.create(username="retry-change")
    chat = ConversationSession.objects.create(
        user=user, bot_name="kitchen", model="auto/medium", engine_type="openai"
    )
    bot.route(chat)
    bot.reroute(chat, "anthropic/claude-opus-5-5", "retry")
    bot.pick_model(chat, "openai/gpt-6-sol")
    bot.pick_model(chat, "auto/medium")
    chat.refresh_from_db()
    assert bot.route(chat) == "openai/gpt-6-sol"

    bot.reroute(chat, "anthropic/claude-opus-5-5", "retry again")
    monkeypatch.delenv("TEST_ANTHROPIC_KEY")
    chat.refresh_from_db()
    assert bot.route(chat) == "openai/gpt-6-sol"
    chat.refresh_from_db()
    assert "retry_route" not in chat.metadata


@pytest.mark.django_db
def test_chat_model_selection_cannot_switch_to_a_worker_provider(
    django_user_model, monkeypatch
):
    from django_ergo.conversation.models import ConversationSession

    monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test")
    bot = bot_with(Providers.from_dict(worker_provider_data()))
    user = django_user_model.objects.create(username="worker-model-picker")
    chat = ConversationSession.objects.create(
        user=user, bot_name="kitchen", model="openai/gpt-6-sol", engine_type="openai"
    )
    bot.pick_model(chat, "xai-oauth/grok-4")
    chat.refresh_from_db()
    assert chat.model == ""
    assert (chat.engine_type, chat.transport_type) == ("openai", "api")
    assert bot.engine_spec(chat).config["model"] == "gpt-6-luna"
