"""The experimental decisions plugin: an OpenAI Decisions API tier router."""

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
            "name": "tier",
            "choice": choice,
            "confidence": confidence,
            "probabilities": [],
        }
        return Decided(answers={"tier": answer}, input_tokens=120)

    plugin.decide = decide
    return asked


def chat(user, model="auto/medium", **kwargs):
    return ConversationSession.objects.create(
        user=user, bot_name="kitchen", model=model, engine_type="claude", **kwargs
    )


@pytest.mark.django_db(transaction=True)
async def test_the_router_picks_a_tier_and_the_rules_pick_its_model(
    django_user_model,
):
    from asgiref.sync import sync_to_async

    bot = decisions_bot(instructions="Recipes are easy.")
    plugin = bot.plugin("decisions")
    asked = answering(plugin, "low", 0.82)
    user = await django_user_model.objects.acreate(username="lee")
    session = await sync_to_async(chat)(
        user, metadata={"routed_model": "claude/claude-opus-5-5"}
    )
    await sync_to_async(record_usage_windows)(
        "claude", {"five_hour": {"used": 90, "resets_at": soon()}}
    )

    pick = await bot.plugin_route(session, "thanks, merge it")
    # Claude is over its 5-hour limit, so the low tier's rule pick is Luna.
    assert (pick.tier, pick.model) == ("low", "chatgpt/gpt-6-luna")
    assert pick.details["confidence"] == 0.82
    assert pick.details["tiers"] == ["low", "medium"]  # no high tier listed
    (call,) = asked
    assert call["input"] == "thanks, merge it"  # no earlier reply yet
    (question,) = call["questions"]
    assert question["type"] == "choice"
    assert 'usual\ntier is "medium"' in question["instructions"]
    assert "claude: 5-hour window 90% used (limit 85%)" in question["instructions"]
    assert "Recipes are easy." in question["instructions"]
    low, medium = question["choices"]
    assert low["value"] == "low"
    assert "Models: claude/claude-sonnet-5-5, chatgpt/gpt-6-luna." in low["description"]
    assert "Would use chatgpt/gpt-6-luna now." in low["description"]
    assert "The chat's usual tier." in medium["description"]
    assert "current model is in this tier" in medium["description"]

    assert await sync_to_async(bot.route)(session, pick) == "chatgpt/gpt-6-luna"
    switch = await RoutingSwitch.objects.aget()
    assert switch.tier == "low"
    assert switch.reason == "Decisions router picked the low tier (82% confident)"
    # The chat runs on the other tier's model until the next pick.
    await session.arefresh_from_db()
    assert bot.engine_spec(session).config["model"] == "gpt-6-luna"


@pytest.mark.django_db(transaction=True)
async def test_the_router_reads_the_previous_reply(django_user_model):
    from asgiref.sync import sync_to_async

    bot = decisions_bot(context_chars=12)
    asked = answering(bot.plugin("decisions"), "high")
    user = await django_user_model.objects.acreate(username="lee")
    session = await sync_to_async(chat)(user)
    await StructuredCall.objects.acreate(
        session=session,
        kind="chat_reply",
        status="completed",
        response={"type": "message", "text": "Shall I redesign the schema?"},
    )
    await bot.plugin_route(session, "go ahead")
    assert asked[0]["input"] == (
        "The bot's previous reply:\n… the schema?\n\nNew message:\ngo ahead"
    )


@pytest.mark.django_db(transaction=True)
async def test_an_unsure_router_keeps_the_chats_tier(django_user_model):
    from asgiref.sync import sync_to_async

    bot = decisions_bot()
    plugin = bot.plugin("decisions")
    user = await django_user_model.objects.acreate(username="lee")
    session = await sync_to_async(chat)(user)

    answering(plugin, "low", confidence=0.2)  # not sure enough
    pick = await bot.plugin_route(session, "hi")
    assert (pick.tier, pick.model) == ("medium", "claude/claude-opus-5-5")
    assert pick.details["picked_tier"] == "low"
    assert "unsure (20%)" in pick.reason
    answering(plugin, "huge")  # not a tier on offer
    assert await bot.plugin_route(session, "hi") is None
    answering(plugin, httpx2.ConnectError("down"))
    assert await bot.plugin_route(session, "hi") is None

    fixed = await sync_to_async(chat)(user, model="claude/claude-opus-5-5")
    asked = answering(plugin, "low")
    assert await bot.plugin_route(fixed, "hi") is None
    assert asked == []  # a chat on a fixed model isn't routed


@pytest.mark.django_db
def test_offered_tiers_follow_the_config_and_limits():
    from django_ergo.bots.routing import route_request

    providers = found()
    request = route_request(providers, "medium")
    plugin = decisions_bot(tiers={"low": "Small talk"}).plugin("decisions")
    (low,) = plugin.router_choices(request)
    assert low["description"].startswith("For Small talk.")

    for name in ("claude", "chatgpt"):
        record_usage_windows(name, {"weekly": {"used": 99, "resets_at": soon(48)}})
    request = route_request(providers, "medium")
    plugin = decisions_bot(enforce_limits=True).plugin("decisions")
    # Low's models are all over a limit; medium still has the API key.
    assert [c["value"] for c in plugin.router_choices(request)] == ["medium"]


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
    answering(bot.plugin("decisions"), "low", 0.75)
    user = await django_user_model.objects.acreate(username="lee")
    session = await sync_to_async(chat)(user)

    result = await bot.ask(session, "Plan next week's menu")
    assert result.text == "On it."
    await session.arefresh_from_db()
    assert session.metadata["routed_model"] == "claude/claude-sonnet-5-5"
    call = await StructuredCall.objects.filter(session=session).alatest("created_at")
    assert call.metadata["routing_pick"]["model"] == "claude/claude-sonnet-5-5"
    assert call.metadata["routing_pick"]["tier"] == "low"
    assert call.metadata["routing_pick"]["source"] == "decisions"
