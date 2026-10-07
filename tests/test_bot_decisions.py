"""The experimental decisions plugin: an OpenAI Decisions API model router."""

from __future__ import annotations

import json

import httpx2
import pytest
from openai import AsyncOpenAI

from django_ergo.bots.definition import BotDefinition
from django_ergo.bots.registry import BotRegistry
from django_ergo.bots.routing import record_usage_windows
from django_ergo.bots.runtime import Bot
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import RoutingSwitch
from django_ergo.conversation.models import StructuredCall
from django_ergo.plugins.decisions import Decided
from tests.test_bot_routing import found
from tests.test_bot_routing import soon
from tests.test_bots import say
from tests.test_conversation_structured import claude_engine


def decisions_bot(engine=None, **config) -> Bot:
    definition = BotDefinition.from_dict(
        {"name": "kitchen", "plugins": [{"name": "decisions", **config}]}
    )
    bot = Bot(definition, engine_factory=(lambda: engine) if engine else None)
    registry = BotRegistry()
    registry.providers = found()
    registry.add(bot)
    return bot


def answering(plugin, choice, confidence=0.9):
    """Replace the plugin's API call with one answer; returns what it was asked."""
    asked = []

    async def decide(input, questions):  # noqa: A002
        asked.append({"input": input, "questions": questions})
        if isinstance(choice, Exception):
            raise choice
        answer = {
            "type": "choice",
            "name": "model",
            "choice": choice,
            "confidence": confidence,
            "probabilities": [],
        }
        return Decided(answers={"model": answer}, input_tokens=120)

    plugin.decide = decide
    return asked


def chat(user, model="auto/medium", **kwargs):
    return ConversationSession.objects.create(
        user=user, bot_name="kitchen", model=model, engine_type="claude", **kwargs
    )


@pytest.mark.django_db(transaction=True)
async def test_the_router_picks_from_the_tier_told_usage_and_priorities(
    django_user_model,
):
    from asgiref.sync import sync_to_async

    bot = decisions_bot(instructions="Recipes are easy.")
    plugin = bot.plugin("decisions")
    asked = answering(plugin, "chatgpt/gpt-6-sol", 0.82)
    user = await django_user_model.objects.acreate(username="lee")
    session = await sync_to_async(chat)(
        user, metadata={"routed_model": "claude/claude-opus-5-5"}
    )
    await sync_to_async(record_usage_windows)(
        "claude", {"five_hour": {"used": 90, "resets_at": soon()}}
    )

    pick = await bot.plugin_route(session, "Refactor the billing module")
    assert pick.model == "chatgpt/gpt-6-sol"
    assert pick.details["confidence"] == 0.82
    assert pick.details["candidates"] == [
        "claude/claude-opus-5-5",
        "chatgpt/gpt-6-sol",
        "openai/gpt-6-sol",
    ]
    (call,) = asked
    assert call["input"] == "Refactor the billing module"
    (question,) = call["questions"]
    assert question["type"] == "choice"
    assert '"medium" tier' in question["instructions"]
    assert "claude: 5-hour window 90% used (limit 85%)" in question["instructions"]
    assert "Recipes are easy." in question["instructions"]
    opus = question["choices"][0]["description"]
    assert "The chat's current model." in opus
    assert "Over its limit: claude 5-hour window at 90% (limit 85%)" in opus
    assert "billed per token" in question["choices"][2]["description"]

    assert await sync_to_async(bot.route)(session, pick) == "chatgpt/gpt-6-sol"
    switch = await RoutingSwitch.objects.aget()
    assert switch.reason == "Decisions router picked it (82% confident)"


@pytest.mark.django_db(transaction=True)
async def test_the_rules_decide_when_the_router_cant(django_user_model):
    from asgiref.sync import sync_to_async

    bot = decisions_bot()
    plugin = bot.plugin("decisions")
    user = await django_user_model.objects.acreate(username="lee")
    session = await sync_to_async(chat)(user)

    answering(plugin, "chatgpt/gpt-6-sol", confidence=0.2)  # not sure enough
    assert await bot.plugin_route(session, "hi") is None
    answering(plugin, "claude/claude-sonnet-5-5")  # not in this tier
    assert await bot.plugin_route(session, "hi") is None
    answering(plugin, httpx2.ConnectError("down"))
    assert await bot.plugin_route(session, "hi") is None

    fixed = await sync_to_async(chat)(user, model="claude/claude-opus-5-5")
    asked = answering(plugin, "chatgpt/gpt-6-sol")
    assert await bot.plugin_route(fixed, "hi") is None
    assert asked == []  # a chat on a fixed model isn't routed


@pytest.mark.django_db
def test_enforce_limits_only_offers_models_under_them():
    from django_ergo.bots.routing import route_request

    providers = found()
    record_usage_windows("claude", {"five_hour": {"used": 90, "resets_at": soon()}})
    request = route_request(providers, "medium")
    bot = decisions_bot(enforce_limits=True)
    values = [c["value"] for c in bot.plugin("decisions").router_choices(request)]
    assert values == ["chatgpt/gpt-6-sol", "openai/gpt-6-sol"]


async def test_decide_calls_the_decisions_endpoint():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "model": "gpt-6-luna",
                "answers": [{"type": "predicate", "probability": 0.7}],
                "usage": {
                    "input_tokens": 41,
                    "input_tokens_details": {
                        "cache_write_tokens": 0,
                        "cached_tokens": 0,
                    },
                    "output_tokens": 0,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 41,
                },
            },
        )

    plugin = decisions_bot().plugin("decisions")
    plugin._client = AsyncOpenAI(
        api_key="test",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    question = {"type": "predicate", "name": "urgent", "instructions": "Is it urgent?"}
    decided = await plugin.decide("The walk-in is warm", [question])
    assert decided.answers["urgent"]["probability"] == 0.7  # named by its question
    assert decided.input_tokens == 41
    assert seen["url"] == "https://api.openai.com/v1/decisions"
    assert seen["body"] == {
        "model": "gpt-6-luna",
        "input": "The walk-in is warm",
        "questions": [question],
    }


def test_no_api_key_means_no_client(monkeypatch):
    from django_ergo.plugins.decisions import DecisionsError

    monkeypatch.delenv("ERGO_TEST_DECISIONS_KEY", raising=False)
    plugin = decisions_bot(api_key_env="ERGO_TEST_DECISIONS_KEY").plugin("decisions")
    with pytest.raises(DecisionsError, match="ERGO_TEST_DECISIONS_KEY isn't set"):
        _ = plugin.client


@pytest.mark.django_db(transaction=True)
async def test_a_turn_keeps_the_routers_pick_on_its_record(django_user_model):
    from asgiref.sync import sync_to_async

    bot = decisions_bot(engine=claude_engine(say("On it.")))
    answering(bot.plugin("decisions"), "chatgpt/gpt-6-sol", 0.75)
    user = await django_user_model.objects.acreate(username="lee")
    session = await sync_to_async(chat)(user)

    result = await bot.ask(session, "Plan next week's menu")
    assert result.text == "On it."
    await session.arefresh_from_db()
    assert session.metadata["routed_model"] == "chatgpt/gpt-6-sol"
    call = await StructuredCall.objects.filter(session=session).alatest("created_at")
    assert call.metadata["routing_pick"]["model"] == "chatgpt/gpt-6-sol"
    assert call.metadata["routing_pick"]["source"] == "decisions"
