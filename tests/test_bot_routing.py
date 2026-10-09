from __future__ import annotations

import shutil
import time
from types import SimpleNamespace

import pytest
import yaml

from django_ergo.bots import routing
from django_ergo.bots.definition import BotDefinition
from django_ergo.bots.providers import Providers
from django_ergo.bots.providers import ProvidersError
from django_ergo.bots.providers import load_providers
from django_ergo.bots.registry import BotRegistry
from django_ergo.bots.routing import claude_windows
from django_ergo.bots.routing import codex_windows
from django_ergo.bots.routing import pick_agent
from django_ergo.bots.routing import pick_model
from django_ergo.bots.routing import record_usage_windows
from django_ergo.bots.runtime import Bot
from django_ergo.conversation.models import RoutingPolicy

SH = shutil.which("sh") or "sh"
PROVIDERS = f"""
default: auto/medium
providers:
  claude:
    type: claude
    transport: cli
    config: {{command: {SH}}}
    models: [claude-sonnet-5-5, claude-opus-5-5]
  chatgpt:
    type: openai
    transport: cli
    config: {{command: {SH}}}
    models: [gpt-6-luna, gpt-6-sol]
  openai:
    type: openai
    models: [gpt-6-sol]
tiers:
  small: [claude/claude-sonnet-5-5, chatgpt/gpt-6-luna]
  medium: [claude/claude-opus-5-5, chatgpt/gpt-6-sol, openai/gpt-6-sol]
agents:
  large:
    - {{agent: claude, model: claude-opus-5-5, effort: high, provider: claude}}
    - {{agent: codex, model: gpt-6-sol, effort: high, provider: chatgpt}}
routing:
  limits:
    - {{provider: claude, window: five_hour, max_used: 85}}
    - {{provider: chatgpt, window: weekly, max_used: 80}}
"""


def found(tmp_path=None, text="") -> Providers:
    if tmp_path is None:
        return Providers.from_dict(yaml.safe_load(PROVIDERS))
    (tmp_path / "providers.yaml").write_text(PROVIDERS)
    if text:
        (tmp_path / "routing.md").write_text(text)
    return load_providers([tmp_path])


def soon(hours=1.0) -> float:
    return time.time() + hours * 3600


def test_tiers_agents_and_rules_parse():
    providers = found()
    assert providers.knows("auto/medium") and providers.knows("auto/large")
    assert providers.routing.agents["large"][1].agent == "codex"
    assert providers.routing.rules.limits[0].max_used == 85
    data = yaml.safe_load(PROVIDERS)
    data["agents"]["large"][0]["provider"] = "openai"  # an API key: not for agents
    with pytest.raises(ProvidersError, match="subscription"):
        Providers.from_dict(data)
    data = yaml.safe_load(PROVIDERS)
    data["tiers"]["huge"] = ["claude/claude-opus-5-5"]
    assert Providers.from_dict(data).knows("auto/huge")


def test_usage_windows_from_each_cli():
    codex = codex_windows(
        {"primary": {"usedPercent": 71, "windowDurationMins": 10080, "resetsAt": 5}}
    )
    assert set(codex) == {"weekly"}  # primary is the sole 7-day window now
    assert codex["weekly"]["used"] == 71
    assert codex["weekly"]["remaining"] == 29
    assert codex["weekly"]["resets_at"] == 5
    claude = claude_windows(
        {
            "status": "allowed_warning",
            "rateLimitType": "five_hour",
            "utilization": 0.9,
            "resetsAt": 7,
            "unifiedWindows": {
                "seven_day": {"utilization": 0.25, "resetsAt": 9},
                "seven_day_fable": {"utilization": 0.8, "resetsAt": 10},
            },
        }
    )
    assert set(claude) == {"five_hour", "weekly", "weekly_fable"}
    assert claude["five_hour"]["used"] == 90
    assert claude["weekly"]["remaining"] == 75
    assert claude["weekly_fable"]["remaining"] == 20
    assert claude["weekly_fable"]["model"] == "fable"
    assert claude["weekly_fable"]["resets_at"] == 10
    rejected = claude_windows({"status": "rejected", "rateLimitType": "five_hour"})
    assert rejected["five_hour"]["used"] == 100
    assert rejected["five_hour"]["remaining"] == 0


def test_unknown_windows_and_unknown_utilization_are_preserved():
    codex = codex_windows({"primary": {"windowDurationMins": 60}})
    assert set(codex) == {"minutes_60"}
    assert codex["minutes_60"]["used"] is None
    claude = claude_windows({"unifiedWindows": {"daily": {"resetsAt": 5}}})
    assert set(claude) == {"daily"}
    assert claude["daily"]["remaining"] is None
    assert codex_windows({}) == claude_windows({}) == {}


@pytest.mark.django_db
def test_authoritative_snapshot_removes_obsolete_windows_partial_event_merges():
    record_usage_windows("chatgpt", {"five_hour": {"used": 99, "resets_at": soon()}})
    record_usage_windows(
        "chatgpt",
        codex_windows({"primary": {"usedPercent": 10, "windowDurationMins": 10080}}),
        full_snapshot=True,
    )
    assert routing.current_usage()["chatgpt"] == {"weekly": 10}
    record_usage_windows("chatgpt", {"daily": {"used": None}})
    assert routing.current_usage()["chatgpt"] == {"weekly": 10}
    report = routing.routing_report(found())
    windows = next(p["windows"] for p in report["providers"] if p["name"] == "chatgpt")
    assert set(windows) == {"weekly", "daily"}
    record_usage_windows("chatgpt", {}, full_snapshot=True)
    assert "chatgpt" not in routing.current_usage()


@pytest.mark.django_db
def test_fable_window_only_blocks_fable_models_for_chats_and_agents():
    from django_ergo.bots.routing import AgentChoice

    data = yaml.safe_load(PROVIDERS)
    data["providers"]["claude"]["models"].append("claude-fable-5-1")
    data["tiers"]["large"] = ["claude/claude-fable-5-1", "claude/claude-opus-5-5"]
    data["agents"]["large"] = [
        {"agent": "omp", "provider": "claude", "model": f"anthropic/{model}"}
        for model in ("claude-fable-5-1", "claude-opus-5-5")
    ]
    data["routing"]["limits"].append(
        {"provider": "claude", "window": "weekly_fable", "max_used": 80}
    )
    providers = Providers.from_dict(data)
    record_usage_windows(
        "claude",
        claude_windows(
            {
                "unifiedWindows": {
                    "five_hour": {"utilization": 0.1, "resetsAt": soon()},
                    "seven_day": {"utilization": 0.3, "resetsAt": soon(48)},
                    "seven_day_fable": {"utilization": 0.8, "resetsAt": soon(48)},
                }
            }
        ),
        full_snapshot=True,
    )
    assert (
        pick_model(providers, "large", current="claude/claude-fable-5-1")
        == "claude/claude-opus-5-5"
    )
    assert pick_agent(providers, "large") == AgentChoice(
        agent="omp", model="anthropic/claude-opus-5-5", provider="claude"
    )
    report = routing.routing_report(providers)
    high = next(t for t in report["tiers"] if t["name"] == "large")
    assert [c["state"] for c in high["candidates"]] == ["skip", "pick"]
    assert (
        routing.retry_model(
            providers, "high", "claude/claude-fable-5-1", "usage limit reached"
        )
        == "claude/claude-opus-5-5"
    )
    record_usage_windows(
        "claude", {"weekly_fable": {"used": 99, "resets_at": time.time() - 1}}
    )
    assert pick_model(providers, "large") == "claude/claude-fable-5-1"
    record_usage_windows("claude", {"weekly": {"used": 99, "resets_at": soon()}})
    assert pick_model(providers, "medium") == "chatgpt/gpt-6-sol"


@pytest.mark.django_db
def test_claude_first_until_its_five_hour_window_runs_low():
    providers = found()
    assert pick_model(providers, "medium") == "claude/claude-opus-5-5"
    record_usage_windows("claude", {"five_hour": {"used": 86, "resets_at": soon()}})
    assert pick_model(providers, "medium") == "chatgpt/gpt-6-sol"
    # Codex's weekly window is protected too; then the API key.
    record_usage_windows("chatgpt", {"weekly": {"used": 80, "resets_at": soon(48)}})
    assert pick_model(providers, "medium") == "openai/gpt-6-sol"
    # Every candidate over a limit: the subscription with the most room.
    assert pick_model(providers, "small") == "chatgpt/gpt-6-luna"  # 20% left vs 14%
    # Back to Claude once its window resets.
    record_usage_windows(
        "claude", {"five_hour": {"used": 99, "resets_at": time.time() - 1}}
    )
    assert pick_model(providers, "medium") == "claude/claude-opus-5-5"


@pytest.mark.django_db
def test_a_chat_keeps_its_model_while_it_qualifies():
    providers = found()
    record_usage_windows("claude", {"five_hour": {"used": 50, "resets_at": soon()}})
    assert (
        pick_model(providers, "medium", current="chatgpt/gpt-6-sol")
        == "chatgpt/gpt-6-sol"
    )
    record_usage_windows("chatgpt", {"weekly": {"used": 90, "resets_at": soon(48)}})
    assert (
        pick_model(providers, "medium", current="chatgpt/gpt-6-sol")
        == "claude/claude-opus-5-5"
    )


@pytest.mark.django_db
def test_agents_use_subscriptions_only():
    providers = found()
    record_usage_windows("claude", {"five_hour": {"used": 92, "resets_at": soon()}})
    choice = pick_agent(providers, "large")
    assert (choice.agent, choice.model, choice.effort) == ("codex", "gpt-6-sol", "high")
    assert pick_agent(providers, "small").provider == "chatgpt"


@pytest.mark.django_db
def test_routing_md_rules_apply_once_compiled(tmp_path):
    providers = found(tmp_path, "Use Claude until its 5-hour window is half used.")
    assert providers.routing.text.startswith("Use Claude")
    record_usage_windows("claude", {"five_hour": {"used": 60, "resets_at": soon()}})
    assert pick_model(providers, "medium") == "claude/claude-opus-5-5"  # YAML rules
    RoutingPolicy.objects.create(
        source_sha=providers.routing.text_sha,
        rules={
            "limits": [{"provider": "claude", "window": "five_hour", "max_used": 50}]
        },
    )
    assert pick_model(providers, "medium") == "chatgpt/gpt-6-sol"


@pytest.mark.django_db
async def test_compile_routing_stores_the_rules(tmp_path, monkeypatch):
    providers = found(tmp_path, "Keep Codex's weekly window under 60%.")

    async def fake_call(spec, message, **kwargs):
        rules = spec.response_model.model_validate(
            {"limits": [{"provider": "chatgpt", "window": "weekly", "max_used": 60}]}
        )
        return SimpleNamespace(parsed=rules, call=SimpleNamespace(error=""))

    monkeypatch.setattr(
        "django_ergo.conversation.structured.run_structured_call", fake_call
    )
    rules = await routing.compile_routing(providers)
    assert rules.limits[0].max_used == 60
    stored = await RoutingPolicy.objects.aget(source_sha=providers.routing.text_sha)
    assert stored.rules["limits"][0]["provider"] == "chatgpt"


def bot_with(providers: Providers) -> Bot:
    bot = Bot(BotDefinition.from_dict({"name": "kitchen"}))
    registry = BotRegistry()
    registry.providers = providers
    registry.add(bot)
    return bot


def test_auto_chats_resolve_without_the_database():
    bot = bot_with(found())
    spec = bot.engine_spec()  # default auto/medium: the tier's first available
    assert (spec.engine_type, spec.transport_type, spec.config["model"]) == (
        "claude",
        "cli",
        "claude-opus-5-5",
    )
    assert spec.config["provider"] == "claude"
    chat = SimpleNamespace(
        model="auto/low", metadata={"routed_model": "chatgpt/gpt-6-luna"}
    )
    assert bot.engine_spec(chat).config["model"] == "gpt-6-luna"
    assert bot.session_model_ref("auto/low") == "auto/low"
    assert bot.session_model_ref("auto/high") == "auto/high"


@pytest.mark.django_db
def test_route_remembers_the_pick_on_the_chat(django_user_model):
    from django_ergo.conversation.models import ConversationSession

    bot = bot_with(found())
    user = django_user_model.objects.create(username="lee")
    chat = ConversationSession.objects.create(
        user=user, bot_name="kitchen", model="auto/medium", engine_type="claude"
    )
    record_usage_windows("claude", {"five_hour": {"used": 90, "resets_at": soon()}})
    assert bot.route(chat) == "chatgpt/gpt-6-sol"
    chat.refresh_from_db()
    assert chat.metadata["routed_model"] == "chatgpt/gpt-6-sol"
    assert (chat.engine_type, chat.transport_type) == ("openai", "cli")
    fixed = ConversationSession.objects.create(
        user=user, bot_name="kitchen", model="claude/claude-sonnet-5-5"
    )
    assert bot.route(fixed) == ""


@pytest.mark.django_db
def test_route_logs_a_switch_with_the_reason(django_user_model):
    from django_ergo.conversation.models import ConversationSession
    from django_ergo.conversation.models import RoutingSwitch

    bot = bot_with(found())
    user = django_user_model.objects.create(username="lee")
    chat = ConversationSession.objects.create(
        user=user,
        bot_name="kitchen",
        model="auto/medium",
        engine_type="claude",
        metadata={"title": "Menu ideas"},
    )
    assert bot.route(chat) == "claude/claude-opus-5-5"
    assert not RoutingSwitch.objects.exists()  # a first pick isn't a switch
    record_usage_windows("claude", {"five_hour": {"used": 90, "resets_at": soon()}})
    assert bot.route(chat) == "chatgpt/gpt-6-sol"
    switch = RoutingSwitch.objects.get()
    assert (switch.label, switch.tier, switch.from_model, switch.to_model) == (
        "kitchen · Menu ideas",
        "medium",
        "claude/claude-opus-5-5",
        "chatgpt/gpt-6-sol",
    )
    assert switch.reason == "claude 5-hour window at 90% (limit 85%)"


@pytest.mark.django_db
def test_text_saved_on_the_page_replaces_routing_md(tmp_path):
    from django_ergo.conversation.models import RoutingText

    providers = found(tmp_path, "Use Claude until its 5-hour window is half used.")
    assert routing.routing_text(providers.routing).startswith("Use Claude")
    RoutingText.objects.create(text="Codex: weekly under 60%, please.\n")
    text = routing.routing_text(providers.routing)
    assert text == "Codex: weekly under 60%, please."
    record_usage_windows("chatgpt", {"weekly": {"used": 70, "resets_at": soon(48)}})
    record_usage_windows("claude", {"five_hour": {"used": 90, "resets_at": soon()}})
    # Not compiled yet: the YAML limits (chatgpt weekly 80) apply.
    assert pick_model(providers, "small") == "chatgpt/gpt-6-luna"
    RoutingPolicy.objects.create(
        source_sha=routing.text_sha(text),
        rules={"limits": [{"provider": "chatgpt", "window": "weekly", "max_used": 60}]},
    )
    # The compiled rules replace the YAML limits: Claude at 90% is under the
    # default 98%, and Codex is over its 60%.
    assert pick_model(providers, "small") == "claude/claude-sonnet-5-5"
    assert routing.why_over(
        "chatgpt", routing.current_usage(), routing.active_rules(providers.routing)
    ) == ("chatgpt weekly window at 70% (limit 60%)")


@pytest.mark.django_db
def test_routing_report_explains_each_tier():
    from django_ergo.conversation.models import RoutingText

    providers = found()
    record_usage_windows("claude", {"five_hour": {"used": 90, "resets_at": soon()}})
    record_usage_windows("chatgpt", {"weekly": {"used": 40, "resets_at": soon(48)}})
    report = routing.routing_report(providers)

    medium = next(t for t in report["tiers"] if t["name"] == "medium")
    assert medium["picked"] == "chatgpt/gpt-6-sol"
    assert [(c["ref"], c["state"]) for c in medium["candidates"]] == [
        ("claude/claude-opus-5-5", "skip"),
        ("chatgpt/gpt-6-sol", "pick"),
        ("openai/gpt-6-sol", "ok"),
    ]
    assert (
        medium["candidates"][0]["reason"] == "claude 5-hour window at 90% (limit 85%)"
    )
    high = next(t for t in report["agents"] if t["name"] == "large")
    assert [(c["label"], c["state"]) for c in high["candidates"]] == [
        ("claude · claude-opus-5-5 · high", "skip"),
        ("codex · gpt-6-sol · high", "pick"),
    ]

    by_name = {p["name"]: p for p in report["providers"]}
    assert by_name["claude"]["status"] == "skipped"
    assert by_name["chatgpt"]["status"] == "in_use"
    assert by_name["openai"]["status"] == "api_key"
    assert by_name["openai"]["subscription"] is False
    assert by_name["openai"]["windows"] == {}
    assert by_name["claude"]["subscription"] is True
    assert by_name["claude"]["windows"]["five_hour"]["used"] == 90
    assert by_name["claude"]["windows"]["five_hour"]["limit"] == 85
    assert "weekly" not in by_name["claude"]["windows"]
    assert set(by_name["chatgpt"]["windows"]) == {"weekly"}
    assert by_name["chatgpt"]["windows"]["weekly"]["limit"] == 80
    assert report["text_source"] == "" and not report["compiled"]

    RoutingText.objects.create(text="Lean on Claude.")
    report = routing.routing_report(providers)
    assert (report["text"], report["text_source"]) == ("Lean on Claude.", "page")


@pytest.mark.django_db
def test_agent_fallbacks_are_logged(django_user_model):
    from django_ergo.conversation.models import RoutingSwitch

    providers = found()
    choice = pick_agent(providers, "large")
    routing.record_agent_pick(providers, "large", choice, "Orca worker · fix tests")
    assert not RoutingSwitch.objects.exists()  # the first choice
    record_usage_windows("claude", {"five_hour": {"used": 92, "resets_at": soon()}})
    choice = pick_agent(providers, "large")
    routing.record_agent_pick(providers, "large", choice, "Orca worker · fix tests")
    switch = RoutingSwitch.objects.get()
    assert (switch.from_model, switch.to_model) == (
        "claude · claude-opus-5-5 · high",
        "codex · gpt-6-sol · high",
    )
    assert switch.reason == "claude 5-hour window at 92% (limit 85%)"


@pytest.mark.django_db
def test_a_turn_refused_at_its_limit_offers_a_retry_but_never_takes_it(
    django_user_model,
):
    from django_ergo.conversation.models import ConversationSession
    from django_ergo.conversation.models import RoutingSwitch

    bot = bot_with(found())
    user = django_user_model.objects.create(username="lee")
    chat = ConversationSession.objects.create(
        user=user, bot_name="kitchen", model="auto/medium", engine_type="claude"
    )
    assert bot.route(chat) == "claude/claude-opus-5-5"
    assert bot.retry_model(chat, "Claude Code failed: boom") == ""  # not a limit
    refused = "Claude AI usage limit reached|1791200000"
    assert bot.retry_model(chat, refused) == "chatgpt/gpt-6-sol"
    chat.refresh_from_db()
    assert chat.metadata["routed_model"] == "claude/claude-opus-5-5"  # only offered

    bot.reroute(chat, "chatgpt/gpt-6-sol", "retried by hand after: limit")
    chat.refresh_from_db()
    assert chat.metadata["routed_model"] == "chatgpt/gpt-6-sol"
    assert (chat.engine_type, chat.transport_type) == ("openai", "cli")
    assert RoutingSwitch.objects.get().reason == "retried by hand after: limit"
    # It stays while it qualifies, though Claude is first in the tier.
    assert bot.route(chat) == "chatgpt/gpt-6-sol"
    with pytest.raises(ValueError, match="isn't a model in this chat's tier"):
        bot.reroute(chat, "chatgpt/gpt-6-luna", "")

    # Usage that shows the provider over its limit counts too, whatever the error.
    record_usage_windows("chatgpt", {"weekly": {"used": 90, "resets_at": soon(48)}})
    assert bot.retry_model(chat, "Codex failed") == "claude/claude-opus-5-5"
    record_usage_windows("claude", {"five_hour": {"used": 100, "resets_at": soon()}})
    assert bot.retry_model(chat, "Codex failed") == "openai/gpt-6-sol"
    fixed = ConversationSession.objects.create(
        user=user, bot_name="kitchen", model="claude/claude-opus-5-5"
    )
    assert bot.retry_model(fixed, refused) == ""
