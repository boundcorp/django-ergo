"""Acceptance tests for the model pools review (design/MODEL_POOLS_REVIEW.md).

Each test is one of the review's reproducers (R1-R17) inverted: it asserts the
invariant the review asked for instead of the defect it observed. No test
reaches a network or pays for inference; sends are mocked.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model

from django_ergo.bots import pools
from django_ergo.conversation.inference_policy import PolicyError
from django_ergo.conversation.models import InferenceRequest
from tests.test_model_pools import chat
from tests.test_model_pools import environment  # noqa: F401 -- fake keys and CLIs
from tests.test_model_pools import registry_of

User = get_user_model()


def ok_usage(prompt=10, completion=5):
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)


# -- R1: the engine is built from the decision's exact route ---------------------


@pytest.mark.django_db(transaction=True)
def test_r1_paid_auto_route_builds_and_sends_on_the_selected_api_route(monkeypatch):
    _, bots = registry_of()
    bot = bots["devbox"]
    user = User.objects.create(username="engine-match")
    session = chat(bot, user)
    pools.update_preference(user.pk, bot.providers, version=0, active_pool="paid")
    route = bot.route(session)
    decision = session._ergo_decision
    assert route == decision.route
    assert route.startswith(("openai/", "anthropic/"))
    spec = bot.engine_spec(session)
    assert spec.transport_type == "api"
    assert spec.config["provider"] == decision.provider
    session.refresh_from_db()
    assert session.transport_type == "api"  # the saved engine fields agree too
    session._ergo_decision = decision
    engine = bot.make_engine(session)
    assert engine.spend_guard.pool == "paid"
    assert (engine.spend_guard.provider, engine.spend_guard.model) == (
        decision.provider,
        decision.model,
    )
    sent = []

    async def send(*args, **kwargs):
        sent.append(kwargs or args[0])
        return SimpleNamespace(id="mock", usage=ok_usage())

    if decision.provider == "openai":
        monkeypatch.setattr(engine, "_send_completion", send)
        async_to_sync(engine._create_completion)(
            {"model": decision.model, "messages": []}
        )
    else:
        monkeypatch.setattr(engine, "_send_message", send)
        async_to_sync(engine._create_message)(model=decision.model, messages=[])
    assert len(sent) == 1
    assert sent[0]["model"] == decision.model
    row = InferenceRequest.objects.get(billing_mode="metered")
    assert row.selector == decision.route


@pytest.mark.django_db
def test_r1_dispatch_refuses_an_engine_that_differs_from_the_decision():
    _, bots = registry_of()
    bot = bots["devbox"]
    user = User.objects.create(username="engine-mismatch")
    session = chat(bot, user)
    pools.update_preference(user.pk, bot.providers, version=0, active_pool="paid")
    bot.route(session)
    # Another model than the one the pool selected is never built.
    with pytest.raises(PolicyError, match="no pool decision"):
        bot.make_engine(session, "claude/claude-opus-5-5")
    with pytest.raises(PolicyError, match="selected"):
        bot.make_engine(
            session, "claude/claude-opus-5-5", decision=session._ergo_decision
        )
    # A chat turn's engine without a pool decision refuses instead of guessing.
    session._ergo_decision = None
    with pytest.raises(PolicyError, match="no pool decision"):
        bot.make_engine(session)
