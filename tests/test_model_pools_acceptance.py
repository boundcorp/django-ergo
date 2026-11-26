"""Acceptance tests for the model pools review (design/MODEL_POOLS_REVIEW.md).

Each test is one of the review's reproducers (R1-R17) inverted: it asserts the
invariant the review asked for instead of the defect it observed. No test
reaches a network or pays for inference; sends are mocked.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model

from django_ergo.bots import pools
from django_ergo.conversation import spend_guard
from django_ergo.conversation.inference_policy import PolicyError
from django_ergo.conversation.models import InferenceRequest
from django_ergo.pricing import ExactPrice
from django_ergo.pricing import LongContextRates
from tests.test_model_pools import POOLS
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


# -- R4: a metered route on an engine that can't enforce spend is refused -------


METERED_CLI = (
    POOLS.replace(
        "    transport: cli\n    models: [claude-sonnet",
        "    transport: cli\n    billing: metered\n    models: [claude-sonnet",
    )
    .replace(
        "models: [claude-sonnet-5-5, claude-opus-5-5]",
        "models: [{name: claude-sonnet-5-5, price: {input: 1, output: 2}}, "
        "{name: claude-opus-5-5, price: {input: 1, output: 2}}]",
    )
    .replace("    label: Subscriptions", "    label: Subscriptions\n    budget_usd: 1")
)


@pytest.mark.django_db
def test_r4_metered_cli_chat_route_is_refused_before_any_engine():
    _, bots = registry_of(METERED_CLI)
    bot = bots["devbox"]
    user = User.objects.create(username="metered-cli")
    session = chat(bot, user)
    session.model = "claude/claude-opus-5-5"
    with pytest.raises(pools.PoolRefusal) as refused:
        bot.route(session)
    assert refused.value.code == pools.UNGUARDED
    # Even handed a decision, the CLI engine isn't built for a metered route.
    decision = pools.Decision(
        consumer=pools.CHAT,
        pool="subs",
        route="claude/claude-opus-5-5",
        provider="claude",
        model="claude-opus-5-5",
        billing="metered",
    )
    with pytest.raises(PolicyError, match="can't enforce a spend bound"):
        bot.make_engine(session, decision=decision)
    # And the listing never offers it.
    listed = {
        m["id"]: m for p in pools.listing(bot, session)["pools"] for m in p["models"]
    }
    assert not listed["claude/claude-opus-5-5"]["available"]


# -- R9: no opaque SDK retries under a guard ------------------------------------


@pytest.mark.parametrize("engine_name", ["openai", "claude"])
def test_r9_guarded_sends_turn_sdk_retries_off(monkeypatch, engine_name):
    from django_ergo.conversation.engine import automatic_request_retries
    from django_ergo.conversation.engines.claude_api import ClaudeAPIEngine
    from django_ergo.conversation.engines.openai_api import OpenAIAPIEngine

    configured = []

    async def create(**kwargs):
        return SimpleNamespace(usage=None)

    if engine_name == "openai":
        client = SimpleNamespace(
            base_url="https://api.openai.com/v1",
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        )
        engine = OpenAIAPIEngine({"model": "gpt-6.1-sol"})
        send = lambda: engine._send_completion({"model": "gpt-6.1-sol"})  # noqa: E731
    else:
        client = SimpleNamespace(
            base_url="https://api.anthropic.com",
            messages=SimpleNamespace(create=create),
        )
        engine = ClaudeAPIEngine({"model": "claude-opus-5-5"})
        send = lambda: engine._send_message(model="claude-opus-5-5")  # noqa: E731
    client.with_options = lambda **kw: configured.append(kw) or client
    engine.spend_guard = SimpleNamespace()
    monkeypatch.setattr(engine, "_get_client", lambda: client)
    token = automatic_request_retries.set(True)
    try:
        async_to_sync(send)()
    finally:
        automatic_request_retries.reset(token)
    assert configured == [{"max_retries": 0}]


@pytest.mark.django_db(transaction=True)
def test_r9_each_attempt_reserves_on_its_own():
    guard = metered_guard()
    attempts = []

    async def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            msg = "mock uncertain send"
            raise TimeoutError(msg)
        return SimpleNamespace(id="ok", usage=ok_usage())

    with pytest.raises(TimeoutError):
        async_to_sync(guard.call)(request(), "max_completion_tokens", flaky)
    async_to_sync(guard.call)(request(), "max_completion_tokens", flaky)
    rows = list(
        InferenceRequest.objects.filter(billing_mode="metered").order_by("attempt")
    )
    assert [r.attempt for r in rows] == [1, 2]
    assert rows[0].cost_status == "unknown" and rows[0].reserved_usd > 0
    assert rows[1].cost_status == "priced"


# -- R10: malformed or partial usage keeps the hold -----------------------------


def price(**extra):
    return ExactPrice(
        provider="openai",
        model="gpt-6.1-sol",
        source="mock",
        input=Decimal(1),
        cached=Decimal("0.1"),
        output=Decimal(10),
        **extra,
    )


def metered_guard(**extra):
    return spend_guard.MeteredGuard(
        pool="paid",
        provider="openai",
        model="gpt-6.1-sol",
        price=price(),
        budget_usd=Decimal(10),
        **extra,
    )


def request(**extra):
    return {"model": "gpt-6.1-sol", "messages": [], **extra}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "usage",
    [
        SimpleNamespace(),  # present but empty
        SimpleNamespace(prompt_tokens=100),  # partial
        SimpleNamespace(prompt_tokens=-100, completion_tokens=-10),  # negative
        SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=99),
        SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            prompt_tokens_details=SimpleNamespace(cached_tokens=11),
        ),
        None,
    ],
)
def test_r10_unusable_usage_keeps_the_reservation(usage):
    guard = metered_guard()

    async def send():
        return SimpleNamespace(id="mock", usage=usage)

    async_to_sync(guard.call)(request(), "max_completion_tokens", send)
    row = InferenceRequest.objects.get(billing_mode="metered")
    assert row.reserved_usd > 0
    assert row.cost_status == "unknown" and row.cost_usd is None
    assert spend_guard.spent_and_reserved("paid") == (Decimal(0), row.reserved_usd)


def test_r10_complete_usage_with_cache_and_reasoning_is_counted_once():
    usage = spend_guard.OpenAIChatAdapter().usage(
        SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=100,
                completion_tokens=20,
                prompt_tokens_details=SimpleNamespace(cached_tokens=30),
                completion_tokens_details=SimpleNamespace(reasoning_tokens=10),
            )
        )
    )
    got = spend_guard.priced(usage, price())
    assert got["cost_status"] == "priced"
    assert got["cost_usd"] == Decimal("0.000273")  # 70 input + 30 cached + 20 output


# -- R11: output ceiling, exact tariffs, unsupported request classes ------------


@pytest.mark.django_db(transaction=True)
def test_r11_output_is_clamped_to_the_pool_ceiling_before_reserving():
    guard = metered_guard(max_output_tokens=16)
    kwargs = request(max_completion_tokens=32000)
    sent = []

    async def send():
        sent.append(kwargs["max_completion_tokens"])
        return SimpleNamespace(id="mock", usage=None)

    async_to_sync(guard.call)(kwargs, "max_completion_tokens", send)
    assert sent == [16]
    row = InferenceRequest.objects.get(billing_mode="metered")
    bound = spend_guard.OpenAIChatAdapter().bound(request(), 16)
    assert row.reserved_usd == price().worst_case(
        bound.input_tokens, 16, cache_write=False
    )


def anthropic_price(**extra):
    return ExactPrice(
        provider="anthropic",
        model="claude-opus-5-5",
        source="mock",
        input=Decimal(5),
        cached=Decimal("0.5"),
        output=Decimal(25),
        **extra,
    )


def cached_request():
    return {
        "model": "claude-opus-5-5",
        "system": [
            {"type": "text", "text": "x" * 100, "cache_control": {"type": "ephemeral"}}
        ],
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        "max_tokens": 10,
    }


@pytest.mark.django_db(transaction=True)
def test_r11_cache_writes_need_an_exact_rate_and_are_covered_by_the_reserve():
    adapter = spend_guard.AnthropicMessagesAdapter()
    guard = spend_guard.MeteredGuard(
        pool="paid",
        provider="anthropic",
        model="claude-opus-5-5",
        price=anthropic_price(),
        budget_usd=Decimal(10),
    )
    with pytest.raises(spend_guard.SpendDenied, match="cache_write"):
        async_to_sync(guard.call)(cached_request(), adapter, lambda: None)
    assert not InferenceRequest.objects.exists()
    full = anthropic_price(cache_write=Decimal("6.25"))
    bound = adapter.bound(cached_request(), 10)
    assert bound.cache_write
    reserve = full.worst_case(bound.input_tokens, 10, cache_write=True)
    # The whole prompt billed as a cache write still fits the reservation.
    billed = full.exact_cost(0, 0, bound.input_tokens, 10)
    assert billed <= reserve
    # A billed cache write without a rate stays unpriced (the hold is kept).
    usage = spend_guard.Usage(uncached=1, cached=0, cache_write=5, output=1)
    assert spend_guard.priced(usage, anthropic_price())["cost_status"] == "unknown"


def test_r11_long_context_tier_prices_the_whole_request():
    tiered = price(
        long_context=LongContextRates(
            threshold=100, input=Decimal(2), cached=Decimal("0.2"), output=Decimal(15)
        )
    )
    assert tiered.exact_cost(50, 0, 0, 10) == Decimal("0.00015")
    assert tiered.exact_cost(150, 0, 0, 10) == Decimal("0.00045")
    assert tiered.worst_case(150, 10, cache_write=False) == Decimal("0.00045")


@pytest.mark.parametrize(
    "extra",
    [
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "https://x/y.png"}}
                    ],
                }
            ]
        },
        {"messages": [{"role": "user", "content": [{"type": "input_audio"}]}]},
        {"tools": [{"type": "web_search_preview"}]},
        {"web_search_options": {}},
        {"n": 2},
        {"modalities": ["text", "audio"]},
    ],
)
def test_r11_unbounded_request_classes_are_refused(extra):
    with pytest.raises(spend_guard.UnsupportedRequest):
        spend_guard.OpenAIChatAdapter().bound(request(**extra), 100)


def test_r11_anthropic_server_tools_and_images_are_refused():
    adapter = spend_guard.AnthropicMessagesAdapter()
    with pytest.raises(spend_guard.UnsupportedRequest):
        adapter.bound(
            {
                "model": "m",
                "messages": [],
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            },
            100,
        )
    with pytest.raises(spend_guard.UnsupportedRequest):
        adapter.bound(
            {
                "model": "m",
                "messages": [
                    {"role": "user", "content": [{"type": "image", "source": {}}]}
                ],
            },
            100,
        )
    usage = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=1,
            output_tokens=1,
            server_tool_use=SimpleNamespace(web_search_requests=1),
        )
    )
    assert adapter.usage(usage) is None
