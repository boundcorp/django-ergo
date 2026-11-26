"""Acceptance tests for the model pools review (design/MODEL_POOLS_REVIEW.md).

Each test is one of the review's reproducers (R1-R17) inverted: it asserts the
invariant the review asked for instead of the defect it observed. No test
reaches a network or pays for inference; sends are mocked.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.utils import timezone

from django_ergo.bots import pools
from django_ergo.bots.providers import parse_providers
from django_ergo.conversation import spend_guard
from django_ergo.conversation.inference_policy import PolicyError
from django_ergo.conversation.models import InferenceRequest
from django_ergo.pricing import ExactPrice
from django_ergo.pricing import LongContextRates
from tests.test_agent_launches import launch_env  # noqa: F401 -- fake Orca
from tests.test_bot_workers import workers  # noqa: F401 -- launch_env needs it
from tests.test_model_pools import POOLS
from tests.test_model_pools import chat
from tests.test_model_pools import environment  # noqa: F401 -- fake keys and CLIs
from tests.test_model_pools import grant
from tests.test_model_pools import registry_of
from tests.test_model_pools import set_chat
from tests.test_model_pools import set_pref
from tests.test_model_pools import set_state

User = get_user_model()


def ok_usage(prompt=10, completion=5):
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)


# -- R1: the engine is built from the decision's exact route ---------------------


@pytest.mark.django_db(transaction=True)
def test_r1_paid_auto_route_builds_and_sends_on_the_selected_api_route(monkeypatch):
    _, bots = registry_of()
    bot = bots["devbox"]
    user = User.objects.create(username="lee")
    session = chat(bot, user)
    set_pref(user.pk, bot.providers, active_pool="paid")
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
    user = User.objects.create(username="lee")
    session = chat(bot, user)
    set_pref(user.pk, bot.providers, active_pool="paid")
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
    .replace(
        "    label: Subscriptions",
        "    label: Subscriptions\n    owner: lee\n    budget_usd: 1\n    expires: 2099-01-01",
    )
)


@pytest.mark.django_db
def test_r4_metered_cli_chat_route_is_refused_before_any_engine():
    _, bots = registry_of(METERED_CLI)
    bot = bots["devbox"]
    user = User.objects.create(username="lee")
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


@pytest.mark.parametrize(
    "consumer",
    [pools.CHAT, pools.TITLE, pools.COMPACTION, pools.BROWSER, pools.BACKGROUND],
)
@pytest.mark.parametrize("ref", ["auto/medium", "claude/claude-opus-5-5"])
@pytest.mark.django_db
def test_r4_metered_cli_route_refuses_unguarded_for_every_consumer(consumer, ref):
    """The automatic-entry gate (N2) doesn't mask R4: a pool whose metered
    routes no engine can guard isn't a paid entry boundary, and every
    consumer gets the ``unguarded`` refusal the UI labels, before any send."""
    _, bots = registry_of(METERED_CLI)
    bot = bots["devbox"]
    user = User.objects.create(username="lee")
    session = chat(bot, user)
    if ref.startswith("auto/"):
        # Automatic routing passes over the unguarded metered routes.
        try:
            decision = pools.select_route(bot, session, ref=ref, consumer=consumer)
        except pools.PoolRefusal as refused:
            assert refused.code == pools.UNGUARDED  # noqa: PT017
        else:
            assert not decision.metered and decision.provider != "claude"
        return
    with pytest.raises(pools.PoolRefusal) as refused:
        pools.select_route(bot, session, ref=ref, consumer=consumer)
    assert refused.value.code == pools.UNGUARDED


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
def test_r9_each_attempt_reserves_on_its_own(held):
    guard = metered_guard(held)
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


@pytest.fixture
def held(db):
    """lee's grant on the paid pool's gpt-6.1-sol route."""
    lee, _ = User.objects.get_or_create(username="lee")
    return grant(parse_providers(POOLS), lee, routes=["openai/gpt-6.1-sol"])


def metered_guard(held, **extra):
    return spend_guard.MeteredGuard(
        pool="paid",
        provider="openai",
        model="gpt-6.1-sol",
        grant_id=held.pk,
        user_id=held.grantee_id,
        price=price(),
        current=pools.SendAuthority(parse_providers(POOLS)),
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
def test_r10_unusable_usage_keeps_the_reservation(usage, held):
    guard = metered_guard(held)

    async def send():
        return SimpleNamespace(id="mock", usage=usage)

    async_to_sync(guard.call)(request(), "max_completion_tokens", send)
    row = InferenceRequest.objects.get(billing_mode="metered")
    assert row.reserved_usd > 0
    assert row.cost_status == "unknown" and row.cost_usd is None
    assert spend_guard.spent_and_reserved(held.pk) == (Decimal(0), row.reserved_usd)


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
def test_r11_output_is_clamped_to_the_pool_ceiling_before_reserving(held):
    guard = metered_guard(held, max_output_tokens=16)
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
def test_r11_cache_writes_need_an_exact_rate_and_are_covered_by_the_reserve(held):
    adapter = spend_guard.AnthropicMessagesAdapter()
    guard = spend_guard.MeteredGuard(
        pool="paid",
        provider="anthropic",
        model="claude-opus-5-5",
        grant_id=held.pk,
        user_id=held.grantee_id,
        price=anthropic_price(),
        current=pools.SendAuthority(parse_providers(POOLS)),
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


# -- R2: pools and spend authority are user-owned ------------------------------


@pytest.mark.django_db
def test_r2_another_user_cannot_select_or_spend_on_an_owned_pool():
    _, bots = registry_of()
    bot = bots["devbox"]
    lee = User.objects.create(username="lee")
    stranger = User.objects.create(username="not-the-sponsor")
    session = chat(bot, stranger)
    with pytest.raises(PermissionError):
        pools.update_preference(
            stranger.pk, bot.providers, version=0, active_pool="paid"
        )
    with pytest.raises(PermissionError):
        set_chat(session, "paid", bot.providers)
    # The pool isn't even visible to them, and auto never reaches it.
    assert [p["name"] for p in pools.listing(bot, session)["pools"]] == ["subs"]
    assert pools.select_route(bot, session, ref="auto/medium").pool == "subs"
    # A stale preference (written behind the API) still mints nothing.
    from django_ergo.conversation.models import ModelPoolPreference

    ModelPoolPreference.objects.create(
        key=pools.preference_key(stranger.pk), user=stranger, active_pool="paid"
    )
    with pytest.raises(pools.PoolRefusal) as refused:
        pools.select_route(bot, session, ref="auto/medium")
    assert refused.value.code == pools.NOT_AUTHORIZED
    # A guard for lee's grant refuses another user's send.
    own = pools.snapshot(bot.providers, lee.pk).get("paid").grant
    guard = spend_guard.MeteredGuard(
        pool="paid",
        provider="openai",
        model="gpt-6.1-sol",
        grant_id=own.pk,
        user_id=stranger.pk,
        price=price(),
        current=pools.SendAuthority(bot),
    )
    with pytest.raises(spend_guard.SpendDenied, match="another user"):
        async_to_sync(guard.call)(request(), "max_completion_tokens", lambda: None)


@pytest.mark.django_db
def test_r2_an_explicit_shared_grant_is_scoped_and_private():
    _, bots = registry_of()
    bot = bots["devbox"]
    providers = bot.providers
    lee = User.objects.create(username="lee")
    friend = User.objects.create(username="friend")
    other = User.objects.create(username="other")
    # Only the owner (or an admin) issues; a grant needs an end date.
    with pytest.raises(PermissionError):
        pools.issue_grant(
            providers,
            "paid",
            issuer_id=friend.pk,
            grantee_id=friend.pk,
            budget_usd=5,
            expires_at=timezone.now() + dt.timedelta(days=1),
        )
    with pytest.raises(ValueError, match="future"):
        pools.issue_grant(
            providers,
            "paid",
            issuer_id=lee.pk,
            grantee_id=friend.pk,
            budget_usd=5,
            expires_at=timezone.now() - dt.timedelta(days=1),
        )
    shared = grant(providers, friend, budget="3", routes=["openai/gpt-6.1-sol"])
    session = chat(bot, friend)
    set_pref(friend.pk, providers, active_pool="paid")
    decision = pools.select_route(bot, session, ref="auto/medium")
    assert decision.route == "openai/gpt-6.1-sol"  # only the granted route
    assert decision.grant_id == shared.pk and decision.budget_usd == 3
    # The grantee sees their own grant, not lee's spend or other grants.
    row = next(p for p in pools.listing(bot, session)["pools"] if p["name"] == "paid")
    assert row["grant"]["id"] == str(shared.pk)
    assert row["grants"] == []
    assert row["budget_usd"] == "3.0000"
    # Ledgers are per grant: lee's spend never counts against friend's grant.
    own = pools.snapshot(providers, lee.pk).get("paid").grant
    spend_guard.reserve(
        grant_id=own.pk,
        provider="openai",
        model="gpt-6.1-sol",
        amount=Decimal(2),
        user_id=lee.pk,
        fields={},
        current=pools.SendAuthority(bot),
    )
    assert spend_guard.spent_and_reserved(shared.pk) == (Decimal(0), Decimal(0))
    # lee (owner) sees the grants they issued; a third user sees no paid pool.
    owner_row = next(
        p
        for p in pools.settings_view(providers, lee.pk)["pools"]
        if p["name"] == "paid"
    )
    assert [g["id"] for g in owner_row["grants"]] == [str(shared.pk)]
    assert "paid" not in {
        p["name"] for p in pools.settings_view(providers, other.pk)["pools"]
    }
    # Revoked: the grantee loses the pool at the next selection and send.
    pools.revoke_grant(providers, shared.pk, by_id=lee.pk)
    with pytest.raises(pools.PoolRefusal):
        pools.select_route(bot, chat(bot, friend), ref="auto/medium")


def test_r2_metered_pools_need_an_owner_and_a_window():
    from django_ergo.bots.providers import ProvidersError

    with pytest.raises(ProvidersError, match="needs an owner"):
        parse_providers(POOLS.replace("    owner: lee\n", ""))
    with pytest.raises(ProvidersError, match="time-bounded"):
        parse_providers(POOLS.replace("    expires: 2099-12-31\n", ""))


# -- R3: automatic selection respects automatic-entry gates ---------------------


@pytest.mark.django_db
def test_r3_disabling_the_first_pool_stops_at_the_paid_boundary():
    _, bots = registry_of()
    bot = bots["devbox"]
    user = User.objects.create(username="lee")
    session = chat(bot, user)
    set_state("subs", bot.providers, enabled=False)
    snap = pools.snapshot(bot.providers, user.pk, session)
    assert not snap.auto_rollover and not snap.get("paid").allow_auto_entry
    with pytest.raises(pools.PoolRefusal) as refused:
        pools.select_route(bot, session, ref="auto/medium")
    assert refused.value.code == pools.POOL_DISABLED
    assert "picked by hand" in str(refused.value)
    assert any(c["name"] == "paid" for c in refused.value.next_choices)
    # Explicit manual choice still works.
    set_pref(user.pk, bot.providers, active_pool="paid")
    assert pools.select_route(bot, session, ref="auto/medium").pool == "paid"
    # And with the entry gate open, automatic selection may enter it.
    set_pref(user.pk, bot.providers, active_pool="")
    set_state("paid", bot.providers, allow_auto_entry=True)
    assert pools.select_route(bot, session, ref="auto/medium").pool == "paid"


@pytest.mark.django_db
def test_r3_rollover_stops_at_a_switched_off_pool(monkeypatch):
    from django_ergo.bots import routing

    three = POOLS.replace(
        "  - name: paid\n",
        "  - name: spare\n    routes: [xai-oauth]\n    enabled: false\n  - name: paid\n",
    )
    _, bots = registry_of(three)
    bot = bots["devbox"]
    user = User.objects.create(username="lee")
    set_pref(user.pk, bot.providers, auto_rollover=True)
    set_state("paid", bot.providers, allow_auto_entry=True)
    monkeypatch.setattr(
        routing,
        "current_usage",
        lambda now=None: {"claude": {"weekly": 100}, "chatgpt": {"weekly": 100}},
    )
    with pytest.raises(pools.PoolRefusal) as refused:
        pools.select_route(bot, chat(bot, user), ref="auto/medium")
    assert refused.value.code == pools.EXHAUSTED
    assert "switched off" in str(refused.value)


# -- R12: queued work keeps the pool and grant it was queued with ---------------


@pytest.mark.django_db
def test_r12_queued_task_keeps_its_captured_pool(monkeypatch):
    from django_ergo.bots import background
    from django_ergo.conversation.models import ConversationSession

    _, bots = registry_of()
    bot = bots["devbox"]
    user = User.objects.create(username="lee")
    session = chat(bot, user)
    captured = {}
    bot.tasks["probe"] = lambda: pools.select_route(
        bot, ConversationSession.objects.get(pk=session.pk), ref="auto/medium"
    ).pool
    monkeypatch.setattr(
        background.api_settings,
        "BOT_TASK_RUNNER",
        lambda b, n, a, k: captured.update(kwargs=k) or SimpleNamespace(),
    )
    background.BotTasks(bot).start("probe")  # queued under subs
    policy = captured["kwargs"][background.POLICY_KWARG]
    assert policy["pool"] == "subs" and policy["grant"] == ""
    assert "revision" in policy
    set_pref(user.pk, bot.providers, active_pool="paid")  # later, wider choice
    monkeypatch.setattr(background, "find_bot", lambda *a: bot)
    assert background.execute(bot.name, "probe", [], dict(captured["kwargs"])) == "subs"


@pytest.mark.django_db
def test_r12_queued_work_on_a_paid_pool_never_widens(monkeypatch):
    _, bots = registry_of()
    bot = bots["devbox"]
    user = User.objects.create(username="lee")
    session = chat(bot, user)
    set_pref(user.pk, bot.providers, active_pool="paid")
    policy = pools.capture_policy(bot, session)
    assert policy["pool"] == "paid" and policy["grant"]
    own = pools.snapshot(bot.providers, user.pk).get("paid").grant
    pools.revoke_grant(bot.providers, own.pk, by_id=user.pk)
    # A new grant was issued since; the queued job doesn't pick it up.
    grant(bot.providers, user, budget="100")
    with pools.policy_scope(policy), pytest.raises(pools.PoolRefusal):
        pools.select_route(bot, session, ref="auto/medium")
    decision = pools.select_route(bot, session, ref="auto/medium")
    assert decision.revision["grant"] != policy["grant"]


# -- R14: the picker lists what is selectable now -------------------------------


@pytest.mark.django_db
def test_r14_picker_distinguishes_selectable_from_configured(monkeypatch):
    from django_ergo.bots import routing

    _, bots = registry_of()
    bot = bots["devbox"]
    user = User.objects.create(username="lee")
    session = chat(bot, user)
    set_chat(session, "paid", bot.providers)
    set_state("paid", bot.providers, enabled=False)
    assert "openai/gpt-6.1-sol" not in pools.chat_models(bot, session)
    paid = next(p for p in pools.listing(bot, session)["pools"] if p["name"] == "paid")
    model = next(m for m in paid["models"] if m["id"] == "openai/gpt-6.1-sol")
    assert model["configured"] and not model["available"]
    assert model["status"] == pools.POOL_DISABLED
    assert not paid["selectable_now"] and not any(paid["tier_available"].values())
    with pytest.raises(pools.PoolRefusal):
        pools.select_route(bot, session, ref="openai/gpt-6.1-sol")
    # Exhaustion counts too: a route with no capacity isn't selectable now.
    set_state("paid", bot.providers, enabled=True)
    set_chat(session, "", bot.providers)
    monkeypatch.setattr(
        routing, "current_usage", lambda now=None: {"claude": {"weekly": 100}}
    )
    subs = next(p for p in pools.listing(bot, session)["pools"] if p["name"] == "subs")
    claude = next(m for m in subs["models"] if m["id"] == "claude/claude-opus-5-5")
    assert claude["status"] == "no_capacity" and not claude["available"]


@pytest.mark.django_db
def test_r14_pool_only_tiers_pass_model_validation():
    from django_ergo.bots.model_choice import MODEL
    from django_ergo.bots.model_choice import check

    text = POOLS.replace(
        "tiers: {medium: [openai/gpt-6.1-sol, anthropic/claude-opus-5-5]}",
        "tiers: {medium: [openai/gpt-6.1-sol], huge: [anthropic/claude-opus-5-5]}",
    )
    _, bots = registry_of(text)
    assert check(bots["devbox"], "auto/huge", MODEL) == "auto/huge"


# -- R15: scope switches are atomic, versions are required ----------------------


@pytest.mark.django_db
def test_r15_versions_are_required():
    _, bots = registry_of()
    providers = bots["devbox"].providers
    user = User.objects.create(username="lee")
    for call in (
        lambda: pools.update_preference(
            user.pk, providers, version=None, auto_rollover=True
        ),
        lambda: pools.update_pool_state("paid", providers, version=None, enabled=False),
        lambda: pools.set_provider_enabled("claude", providers, False, version=None),
        lambda: pools.set_chat_pool(
            chat(bots["devbox"], user), "subs", providers, version=None
        ),
    ):
        with pytest.raises(ValueError, match="version"):
            call()
    set_pref(user.pk, providers, auto_rollover=True)
    with pytest.raises(pools.VersionConflict):
        pools.update_preference(user.pk, providers, version=0, auto_rollover=False)


@pytest.mark.django_db(transaction=True)
def test_r15_cross_scope_switch_is_one_transaction(monkeypatch):
    from django_ergo.conversation.models import ConversationSession

    _, bots = registry_of()
    bot = bots["devbox"]
    user = User.objects.create(username="lee")
    session = chat(bot, user)
    set_chat(session, "subs", bot.providers)
    stale = pools.chat_revision(session) - 1
    with pytest.raises(pools.VersionConflict):
        pools.switch_pool(
            user.pk,
            bot.providers,
            "paid",
            scope="user",
            session=session,
            version=0,
            chat_version=stale,
        )
    # Neither scope changed.
    assert (pools.preference(user.pk) is None) or pools.preference(
        user.pk
    ).active_pool == ""
    row = ConversationSession.objects.get(pk=session.pk)
    assert row.metadata[pools.SESSION_POOL_KEY] == "subs"
    pools.switch_pool(
        user.pk,
        bot.providers,
        "paid",
        scope="user",
        session=row,
        version=0,
        chat_version=pools.chat_revision(row),
    )
    row.refresh_from_db()
    assert pools.preference(user.pk).active_pool == "paid"
    assert pools.SESSION_POOL_KEY not in row.metadata
    decision = pools.select_route(bot, row, ref="auto/medium")
    assert decision.revision["preference"] == pools.preference(user.pk).version


# -- R16: a rollover is persisted, with an explicit way back ----------------------


@pytest.mark.django_db
def test_r16_rollover_stays_until_the_person_returns(monkeypatch):
    from django_ergo.bots import routing

    _, bots = registry_of()
    bot = bots["devbox"]
    user = User.objects.create(username="lee")
    session = chat(bot, user)
    set_pref(user.pk, bot.providers, auto_rollover=True)
    set_state("paid", bot.providers, allow_auto_entry=True)
    monkeypatch.setattr(
        routing,
        "current_usage",
        lambda now=None: {"claude": {"weekly": 100}, "chatgpt": {"weekly": 100}},
    )
    assert bot.route(session).startswith(("openai/", "anthropic/"))
    pref = pools.preference(user.pk)
    assert (pref.active_pool, pref.rolled_from) == ("paid", "subs")
    listed = pools.listing(bot, session)
    assert listed["selected"] == "paid" and listed["rolled_from"] == "subs"
    # Subscription quota recovers: no invisible return.
    monkeypatch.setattr(routing, "current_usage", lambda now=None: {})
    assert bot.route(session).startswith(("openai/", "anthropic/"))
    # "Use preferred order" returns explicitly.
    set_pref(user.pk, bot.providers, active_pool="")
    assert bot.route(session).startswith(("claude/", "chatgpt/"))


# -- R17: amounts and windows are validated on the server ----------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("budget_usd", "-2"),
        ("budget_usd", "NaN"),
        ("budget_usd", "Infinity"),
        ("budget_usd", True),
        ("per_launch_usd", "-2"),
        ("per_launch_usd", "1e400"),
    ],
)
def test_r17_grant_amounts_must_be_finite_and_non_negative(field, value):
    _, bots = registry_of()
    lee = User.objects.create(username="lee")
    args = {"budget_usd": "5", "per_launch_usd": None, field: value}
    with pytest.raises(ValueError):  # noqa: PT011
        pools.issue_grant(
            bots["devbox"].providers,
            "paid",
            issuer_id=lee.pk,
            grantee_id=lee.pk,
            expires_at=timezone.now() + dt.timedelta(days=1),
            **args,
        )
    with pytest.raises(ValueError, match="budgets and expiry are spend grants"):
        set_state("paid", bots["devbox"].providers, per_launch_usd=-2)


# -- R5: async worker steps run inside their origin ----------------------------


def test_r5_async_worker_step_runs_inside_its_origin(monkeypatch):
    from django_ergo.bots import workers as worker_steps

    seen = []

    async def step():
        seen.append((pools.current_origin(), pools.current_policy()))

    worker = SimpleNamespace(
        bot_name="devbox",
        function="mock",
        args={
            worker_steps.PRIVACY_ARG: ["personal"],
            worker_steps.POLICY_ARG: {"pool": "subs", "grant": ""},
        },
    )
    monkeypatch.setattr(worker_steps, "_claim", lambda _: (worker, ""))
    monkeypatch.setattr(worker_steps, "_notify", lambda _: None)
    monkeypatch.setattr(
        "django_ergo.bots.background.find_bot", lambda *a: SimpleNamespace()
    )
    monkeypatch.setattr(worker_steps, "resolve", lambda *a: step)
    monkeypatch.setattr(
        worker_steps, "WorkerContext", lambda *a: SimpleNamespace(state={})
    )
    monkeypatch.setattr(worker_steps, "_apply_step", lambda *a: "done")
    worker_steps._run("mock")
    assert seen == [(("personal",), {"pool": "subs", "grant": ""})]


# -- R6: summaries leave out sessions under another privacy policy ---------------


@pytest.mark.django_db(transaction=True)
def test_r6_overview_and_thread_list_hide_restricted_sessions():
    from django_ergo.bots import overview
    from django_ergo.bots.orchestrator import ergo_thread_list
    from django_ergo.bots.registry import unreadable
    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.messages import add_message
    from django_ergo.conversation.models import ConversationSession

    _, bots = registry_of()
    bot = bots["devbox"]
    user = User.objects.create(username="history")
    plain = chat(bot, user)
    tainted = chat(bot, user, **{pools.ORIGIN_KEY: ["personal"]})
    async_to_sync(add_message)(
        tainted, "user", [{"block_type": "text", "text": "synthetic private phrase"}]
    )
    ctx = ToolContext(bot=bot, session=plain)
    assert tainted.pk not in {s.session.pk for s in bot.history_sources(plain)}
    text = overview.overview(ctx)
    assert "synthetic private phrase" not in text
    assert str(tainted.pk) not in text
    listed = (
        ergo_thread_list.__wrapped__(ctx)
        if hasattr(ergo_thread_list, "__wrapped__")
        else ergo_thread_list(ctx)
    )
    assert str(tainted.pk) not in str(listed)
    assert set(
        ConversationSession.objects.filter(user=user)
        .exclude(unreadable(bot))
        .values_list("pk", flat=True)
    ) == {plain.pk}
    # Work under the same policy still sees it.
    with pools.origin_scope(["personal"]):
        assert tainted.pk in set(
            ConversationSession.objects.filter(user=user)
            .exclude(unreadable(bot))
            .values_list("pk", flat=True)
        )


# -- R7: memory under the inherited policy -------------------------------------


MEMORY_POLICY = POOLS.replace(
    "private_pools:",
    "privacy:\n  personal:\n    memory: [personal-memory]\nprivate_pools:",
)


def memory_plugin(monkeypatch, settings, text=POOLS, bank="ergonaut"):
    from django_ergo.plugins import hindsight_memory as memory

    settings.HINDSIGHT_URL = "http://mock.invalid"
    _, bots = registry_of(text)
    plugin = memory.HindsightMemoryPlugin(bots["devbox"], {"bank": bank})
    plugin.on_load()
    queued = []
    monkeypatch.setattr(memory._pool, "submit", lambda *args: queued.append(args))
    posts = []
    monkeypatch.setattr(memory, "_post", lambda *a, **k: posts.append(a) or {})
    return memory, plugin, queued, posts


def test_r7_restricted_work_never_reaches_an_unapproved_bank(monkeypatch, settings):
    memory, plugin, queued, posts = memory_plugin(monkeypatch, settings)
    session = SimpleNamespace(id="mock", user_id=1, parent_id=None, metadata={})
    with pools.origin_scope(["personal"]):
        plugin._retain(session, "synthetic private data")
        assert "refused" in plugin._recall(session, "q", timeout=1)
        ctx = SimpleNamespace(session=session)
        toolkit = plugin.make_toolkit(ctx)
        reflected = toolkit.execute_tool("memory_reflect", {"question": "q"})
        assert "refused" in reflected
        assert "Not saved" in toolkit.execute_tool("memory_retain", {"text": "x"})
        assert plugin.always_context_sources(ctx, "hello") == []
    assert queued == [] and posts == []


def test_r7_approved_bank_carries_the_policy_through_the_queue(monkeypatch, settings):
    memory, plugin, queued, posts = memory_plugin(
        monkeypatch, settings, MEMORY_POLICY, bank="personal-memory"
    )
    session = SimpleNamespace(id="mock", user_id=1, parent_id=None, metadata={})
    with pools.origin_scope(["personal"]):
        plugin._retain(session, "synthetic private data")
        ctx = SimpleNamespace(session=session)
        toolkit = plugin.make_toolkit(ctx)
        # Reflect is LLM synthesis: it needs its own approval.
        assert "refused" in toolkit.execute_tool("memory_reflect", {"question": "q"})
    assert len(queued) == 1
    _, bank, item = queued[0]
    assert bank == "personal-memory"
    assert item[memory.PRIVACY_ITEM_KEY] == {
        "origin": ["personal"],
        "banks": ["personal-memory"],
    }
    assert "privacy:personal" in item["tags"]
    # The worker posts it to the approved bank only, without the private key.
    memory.retain_item("ergonaut", dict(item))
    assert posts == []
    memory.retain_item("personal-memory", dict(item))
    assert len(posts) == 1 and memory.PRIVACY_ITEM_KEY not in posts[0][2]["items"][0]


def test_r7_a_policy_bank_is_closed_to_other_work(monkeypatch, settings):
    _, plugin, queued, _ = memory_plugin(
        monkeypatch, settings, MEMORY_POLICY, bank="personal-memory"
    )
    session = SimpleNamespace(id="mock", user_id=1, parent_id=None, metadata={})
    plugin._retain(session, "public data")
    assert queued == []
    assert "refused" in plugin._recall(session, "q", timeout=1)


# -- R8: video and LLM search go through the pool or refuse --------------------


def test_r8_video_refuses_before_sending_with_pools(monkeypatch):
    from contextlib import nullcontext

    from django_ergo.conversation import video
    from django_ergo.plugins.attachments import AttachmentsPlugin

    _, bots = registry_of()
    bot = bots["devbox"]
    plugin = AttachmentsPlugin(bot, {})
    plugin.on_load()
    monkeypatch.setattr(video, "local_path", lambda row: nullcontext("/mock.mp4"))
    sent = []
    monkeypatch.setattr(video, "ask_gemini", lambda *a, **k: sent.append(True))
    session = SimpleNamespace(metadata={pools.SESSION_POOL_KEY: "subs"}, user_id=None)
    ctx = SimpleNamespace(bot=bot, session=session, secret=lambda _: "mock-key")
    row = SimpleNamespace(filename="mock.mp4", size=1, media_type="video/mp4")
    assert "isn't available while pools are configured" in plugin.watch(ctx, row)
    assert sent == []


@pytest.mark.django_db
def test_r8_llm_web_search_needs_the_active_pool(monkeypatch):
    from django_ergo.plugins.web_search import WebSearchPlugin

    _, bots = registry_of()
    bot = bots["devbox"]
    user = User.objects.create(username="lee")
    session = chat(bot, user)
    sent = []
    anthropic = WebSearchPlugin(bot, {"backend": "anthropic"})
    anthropic.on_load()
    monkeypatch.setattr(
        anthropic, "_search_anthropic", lambda *a: sent.append("a") or "x"
    )
    assert anthropic.search("q", session=session).startswith("Search failed")
    claude = WebSearchPlugin(bot, {"backend": "claude_code"})
    claude.on_load()
    monkeypatch.setattr(
        claude, "_search_claude_code", lambda *a: sent.append("c") or "ok"
    )
    assert claude.search("q", session=session) == "ok"  # subs has Claude
    assert sent == ["c"]
    # On a pool without a Claude subscription it refuses before sending.
    set_pref(user.pk, bot.providers, active_pool="paid")
    assert claude.search("q", session=chat(bot, user)).startswith("Search failed")
    assert sent == ["c"]


def test_r8_personal_web_search_uses_brave_when_configured(monkeypatch):
    from django_ergo.plugins.web_search import WebSearchPlugin

    text = POOLS.replace(
        "private_pools:",
        "privacy:\n  personal:\n    web_search: [brave]\nprivate_pools:",
    )
    _, bots = registry_of(text)
    plugin = WebSearchPlugin(bots["devbox"], {"backend": "claude_code"})
    plugin.on_load()
    monkeypatch.setenv(plugin.brave_key_env, "mock-brave")
    monkeypatch.setattr(plugin, "_search_brave", lambda *a: "brave results")
    monkeypatch.setattr(plugin, "_search_claude_code", lambda *a: "llm!")
    with pools.origin_scope(["personal"]):
        assert plugin.search("q") == "brave results"


# -- R13: one migration leaf; pool selection composed with the launch journal ----


def test_r13_one_migration_leaf_after_the_launch_journal():
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(None, ignore_no_migrations=True)
    assert loader.detect_conflicts() == {}
    leaves = [n for n in loader.graph.leaf_nodes() if n[0] == "django_ergo"]
    assert leaves == [("django_ergo", "0060_spend_envelopes")]
    envelopes = loader.disk_migrations[("django_ergo", "0060_spend_envelopes")]
    assert ("django_ergo", "0059_model_pools") in envelopes.dependencies
    migration = loader.disk_migrations[("django_ergo", "0059_model_pools")]
    assert ("django_ergo", "0058_agent_launch_intent") in migration.dependencies


@pytest.mark.django_db(transaction=True)
def test_r13_orca_launch_journals_its_pool_receipt_once(launch_env, monkeypatch):  # noqa: F811
    from django_ergo.conversation.models import AgentLaunch
    from django_ergo.conversation.models import Worker
    from tests.test_agent_launches import start

    selected = []

    def select_agent(bot, session, **kwargs):
        selected.append(kwargs)
        return pools.Decision(
            consumer=pools.CODING,
            pool="subs",
            route="claude/claude-opus-5-5",
            provider="claude",
            model="claude-opus-5-5",
            receipt_id="receipt-1",
            legacy=False,
        )

    monkeypatch.setattr(pools, "select_agent", select_agent)
    first = start(launch_env)
    intent = AgentLaunch.objects.get()
    receipt = intent.watch_state["dispatch_receipt"]
    assert (receipt["pool"], receipt["decision"]) == ("subs", "receipt-1")
    calls = list(launch_env[2])
    # Replaying the same key neither selects (or reserves) again nor launches.
    again = start(launch_env)
    assert again["id"] == first["id"]
    assert len(selected) == 1
    assert launch_env[2] == calls
    assert Worker.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_r12_tasks_queued_from_async_code_capture_their_policy(monkeypatch):
    from django_ergo.bots import background

    _, bots = registry_of()
    bot = bots["devbox"]
    captured = {}
    monkeypatch.setattr(
        background.api_settings,
        "BOT_TASK_RUNNER",
        lambda b, n, a, k: captured.update(kwargs=k) or SimpleNamespace(),
    )
    bot.tasks["probe"] = lambda: None

    async def queue():
        background.BotTasks(bot).start("probe")

    async_to_sync(queue)()
    assert captured["kwargs"][background.POLICY_KWARG]["pool"] == "subs"
