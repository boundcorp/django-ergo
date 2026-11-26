"""Model pools (bots.pools): ordered pools, manual selection, rollover gates,
metered budgets, provider switches, coding confinement and inherited privacy.

No test reaches a network: engines are never asked to send except through
monkeypatched send functions, and agent launches are captured, not run.
"""

from __future__ import annotations

import datetime as dt
import threading
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone

from django_ergo.bots import pools
from django_ergo.bots import routing
from django_ergo.bots.definition import BotDefinition
from django_ergo.bots.providers import ProvidersError
from django_ergo.bots.providers import parse_providers
from django_ergo.bots.registry import BotRegistry
from django_ergo.bots.registry import privacy_readable
from django_ergo.bots.runtime import Bot
from django_ergo.conversation import spend_guard
from django_ergo.conversation.engines.openai_api import OpenAIAPIEngine
from django_ergo.conversation.inference_policy import PolicyError
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import InferenceRequest
from django_ergo.conversation.models import RouteDecision

User = get_user_model()
KIMI = "accounts/fireworks/models/kimi-k3"

POOLS = f"""
default: auto/medium
providers:
  claude:
    type: claude
    transport: cli
    models: [claude-sonnet-5-5, claude-opus-5-5]
  chatgpt:
    type: openai
    transport: cli
    models: [gpt-6.1-sol]
  openai:
    type: openai
    api_key_env: TEST_POOL_OPENAI_KEY
    models:
      - {{name: gpt-6.1-sol, price: {{input: 1, cached: 0.1, output: 10}}}}
      - gpt-6-luna
  anthropic:
    type: claude
    api_key_env: TEST_POOL_ANTHROPIC_KEY
    models:
      - {{name: claude-opus-5-5, price: {{input: 5, output: 25}}}}
  xai-oauth: {{type: omp, transport: cli, account: xai-oauth}}
tiers:
  medium: [claude/claude-opus-5-5, chatgpt/gpt-6.1-sol]
agents:
  medium:
    - {{agent: claude, model: claude-opus-5-5, provider: claude}}
    - {{agent: codex, model: gpt-6.1-sol, provider: chatgpt}}
auto_rollover: false
pools:
  - name: subs
    label: Subscriptions
    routes: [claude, chatgpt, xai-oauth]
  - name: paid
    label: Paid API
    owner: lee
    routes: [openai, anthropic]
    budget_usd: 10
    expires: 2099-12-31
    per_launch_usd: 2
    tiers: {{medium: [openai/gpt-6.1-sol, anthropic/claude-opus-5-5]}}
private_pools:
  personal:
    enabled: true
    provider: fireworks
    allowed_bots: [personal]
    default: heavy
    models:
      heavy: {KIMI}
"""

LEGACY = """
default: auto/medium
providers:
  claude:
    type: claude
    transport: cli
    models: [claude-opus-5-5]
  openai:
    type: openai
    api_key_env: TEST_POOL_OPENAI_KEY
    models: [gpt-6.1-sol]
tiers:
  medium: [claude/claude-opus-5-5]
"""


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.setenv("TEST_POOL_OPENAI_KEY", "sk-test-openai")
    monkeypatch.setenv("TEST_POOL_ANTHROPIC_KEY", "sk-test-anthropic")
    monkeypatch.setenv("FIREWORKS_API_KEY", "fw-test")
    monkeypatch.setattr(
        "django_ergo.conversation.engines.claude_code.claude_installed",
        lambda command="": True,
    )
    monkeypatch.setattr(
        "django_ergo.conversation.engines.codex_cli.codex_installed",
        lambda command="": True,
    )
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(routing, "current_usage", lambda now=None: {})
    monkeypatch.setattr(routing, "active_rules", lambda r: routing.RoutingRules())
    monkeypatch.setattr(
        routing,
        "_eligible_coding_logins",
        lambda providers, candidates, tier, errors_out=None: list(candidates),
    )


def exhaust(monkeypatch, *providers):
    usage = {name: {"weekly": 100} for name in providers}
    monkeypatch.setattr(routing, "current_usage", lambda now=None: usage)


def registry_of(text=POOLS, **engines):
    registry = BotRegistry()
    registry.providers = parse_providers(text)
    out = {}
    for name, engine in (engines or {"devbox": {}}).items():
        bot = Bot(BotDefinition.from_dict({"name": name, "engine": engine or {}}))
        registry.add(bot)
        out[name] = bot
    return registry, out


@pytest.fixture
def user(db):
    return User.objects.create(username="lee")


# Writers with the version last read (they require one).


def set_pref(user_id, providers, **changes):
    current = pools.preference(user_id)
    return pools.update_preference(
        user_id, providers, version=current.version if current else 0, **changes
    )


def set_state(name, providers, **changes):
    from django_ergo.conversation.models import ModelPoolState

    row = ModelPoolState.objects.filter(name=name).first()
    return pools.update_pool_state(
        name, providers, version=row.version if row else 0, **changes
    )


def set_provider(name, providers, enabled):
    from django_ergo.conversation.models import ProviderToggle

    row = ProviderToggle.objects.filter(name=name).first()
    return pools.set_provider_enabled(
        name, providers, enabled, version=row.version if row else 0
    )


def set_chat(session, pool, providers):
    pools.set_chat_pool(session, pool, providers, version=pools.chat_revision(session))


def grant(providers, grantee, *, pool="paid", budget="10", days=7, **extra):
    """A grant from the pool's owner (lee) to ``grantee``."""
    owner = User.objects.get(username="lee")
    return pools.issue_grant(
        providers,
        pool,
        issuer_id=owner.pk,
        grantee_id=grantee.pk,
        budget_usd=budget,
        expires_at=timezone.now() + dt.timedelta(days=days),
        **extra,
    )


def chat(bot, user, **metadata):
    return ConversationSession.objects.create(
        user=user,
        bot_name=bot.name,
        engine_type="claude",
        transport_type="cli",
        metadata=metadata,
    )


# -- config ------------------------------------------------------------------------


def test_legacy_config_synthesizes_one_subscription_pool_without_rollover():
    providers = parse_providers(LEGACY)
    assert providers.pools.legacy
    (pool,) = providers.pools.pools
    assert (pool.name, pool.rollover_out, pool.allow_auto_entry) == (
        "subscription",
        False,
        False,
    )
    assert pool.contains("openai", "gpt-6.1-sol")
    assert providers.pools.auto_rollover is False


def test_pool_defaults_follow_billing_and_metered_routes_need_listed_models():
    providers = parse_providers(POOLS)
    subs, paid = providers.pools.pools
    assert not subs.metered and subs.rollover_out and subs.allow_auto_entry
    assert paid.metered and not paid.rollover_out and not paid.allow_auto_entry
    assert providers.providers["openai"].models["gpt-6.1-sol"].price.output == 10
    bad = POOLS.replace("routes: [openai, anthropic]", "routes: [openai/gpt-9]")
    with pytest.raises(ProvidersError, match="isn't one of openai's models"):
        parse_providers(bad)
    with pytest.raises(ProvidersError, match="unknown keys"):
        parse_providers(POOLS.replace("    per_launch_usd: 2", "    surprise: 1"))


def test_google_provider_type_routes_once_an_engine_is_registered(monkeypatch):
    text = POOLS.replace(
        "  xai-oauth:",
        "  gemini:\n    type: google\n    api_key_env: TEST_POOL_OPENAI_KEY\n"
        "    models: [{name: gemini-3-pro, price: {input: 2, output: 12}}]\n  xai-oauth:",
    ).replace("routes: [openai, anthropic]", "routes: [openai, anthropic, gemini]")
    providers = parse_providers(text)
    snap = pools.Snapshot(providers=providers, pools=[], auto_rollover=False)
    privacy = pools.Privacy()
    problem = pools.route_problem(snap, privacy, pools.CHAT, "gemini", "gemini-3-pro")
    assert problem[0] == pools.UNAVAILABLE  # no ("google", "api") engine here yet
    from django_ergo.conversation import engines

    # Registered without the spend adapter contract: still refused (metered).
    monkeypatch.setitem(
        engines.ENGINE_REGISTRY, ("google", "api"), "tests.test_model_pools.NoAdapter"
    )
    problem = pools.route_problem(snap, privacy, pools.CHAT, "gemini", "gemini-3-pro")
    assert problem[0] == pools.UNGUARDED
    monkeypatch.setitem(
        engines.ENGINE_REGISTRY, ("google", "api"), "tests.test_model_pools.WithAdapter"
    )
    assert (
        pools.route_problem(snap, privacy, pools.CHAT, "gemini", "gemini-3-pro") is None
    )


class NoAdapter:
    spend_adapter = None


class WithAdapter:
    spend_adapter = spend_guard.OpenAIChatAdapter()


# -- legacy behavior is unchanged ------------------------------------------------------


@pytest.mark.django_db
def test_legacy_routing_pins_and_engines_are_unchanged(user):
    _, found = registry_of(LEGACY)
    bot = found["devbox"]
    session = chat(bot, user)
    assert bot.route(session) == "claude/claude-opus-5-5"
    assert routing.pick_model(bot.providers, "medium") == "claude/claude-opus-5-5"
    # An API pin keeps working, with no spend guard (exactly as before pools).
    bot.pick_model(session, "openai/gpt-6.1-sol")
    assert bot.route(session) == ""
    engine = bot.make_engine(session)
    assert isinstance(engine, OpenAIAPIEngine)
    assert engine.spend_guard is None
    assert not RouteDecision.objects.exists()


# -- ordered selection and manual pools -------------------------------------------------


@pytest.mark.django_db
def test_auto_mode_uses_the_first_pool_and_records_a_receipt(user):
    _, found = registry_of()
    bot = found["devbox"]
    session = chat(bot, user)
    assert bot.route(session) in ("claude/claude-opus-5-5", "chatgpt/gpt-6.1-sol")
    route = session.metadata[pools.SESSION_ROUTE_KEY]
    assert (route["pool"], route["mode"], route["billing"]) == (
        "subs",
        "auto",
        "subscription",
    )
    receipt = RouteDecision.objects.get()
    assert (receipt.pool, receipt.outcome, receipt.consumer) == (
        "subs",
        "selected",
        "chat",
    )


@pytest.mark.django_db
def test_manual_pool_user_default_chat_override_and_preferred_order(user):
    _, found = registry_of()
    bot = found["devbox"]
    providers = bot.providers
    session = chat(bot, user)
    set_pref(user.pk, providers, active_pool="paid")
    assert bot.route(session) in ("openai/gpt-6.1-sol", "anthropic/claude-opus-5-5")
    assert session.metadata[pools.SESSION_ROUTE_KEY]["mode"] == "manual"
    # A chat override wins over the user default.
    set_chat(session, "subs", providers)
    assert bot.route(session).partition("/")[0] in ("claude", "chatgpt")
    # Back to the user's default, then "use preferred order".
    set_chat(session, "", providers)
    assert bot.route(session).partition("/")[0] in ("openai", "anthropic")
    pref = pools.preference(user.pk)
    pools.update_preference(user.pk, providers, version=pref.version, active_pool="")
    assert bot.route(session).partition("/")[0] in ("claude", "chatgpt")
    with pytest.raises(pools.VersionConflict):
        pools.update_preference(user.pk, providers, version=0, active_pool="paid")


@pytest.mark.django_db
def test_preferred_order_is_per_user(user):
    _, found = registry_of()
    bot = found["devbox"]
    other = User.objects.create(username="other")
    set_pref(user.pk, bot.providers, order=["paid", "subs"])
    assert bot.route(chat(bot, user)).partition("/")[0] in ("openai", "anthropic")
    assert bot.route(chat(bot, other)).partition("/")[0] in ("claude", "chatgpt")


# -- rollover gates ---------------------------------------------------------------------


GATES = [
    # (auto_rollover, subs rollover_out, paid allow_auto_entry, rolls over)
    (False, True, True, False),
    (True, False, True, False),
    (True, True, False, False),
    (True, True, True, True),
]


@pytest.mark.django_db
@pytest.mark.parametrize(("auto", "out", "entry", "rolls"), GATES)
def test_rollover_needs_all_three_gates(user, monkeypatch, auto, out, entry, rolls):  # noqa: PLR0913
    _, found = registry_of()
    bot = found["devbox"]
    providers = bot.providers
    exhaust(monkeypatch, "claude", "chatgpt")
    set_pref(user.pk, providers, auto_rollover=auto)
    set_state("subs", providers, rollover_out=out)
    set_state("paid", providers, allow_auto_entry=entry)
    session = chat(bot, user)
    if rolls:
        picked = bot.route(session)
        assert picked.partition("/")[0] in ("openai", "anthropic")
        assert session.metadata[pools.SESSION_ROUTE_KEY]["rollover"] == ["subs"]
        return
    with pytest.raises(pools.PoolRefusal) as refused:
        bot.route(session)
    assert refused.value.code == pools.EXHAUSTED
    assert refused.value.pool == "subs"
    assert [c["name"] for c in refused.value.next_choices] == ["paid"]
    stored = session.metadata[pools.SESSION_REFUSAL_KEY]
    assert stored["code"] == pools.EXHAUSTED
    assert stored["next_choices"][0]["name"] == "paid"


@pytest.mark.django_db
@pytest.mark.parametrize("cause", ["disabled", "no_key", "privacy"])
def test_non_exhaustion_errors_never_roll_over(user, monkeypatch, cause):
    _, found = registry_of()
    bot = found["devbox"]
    providers = bot.providers
    set_pref(user.pk, providers, auto_rollover=True)
    set_state("paid", providers, allow_auto_entry=True)
    session = chat(bot, user)
    if cause == "disabled":
        set_provider("claude", providers, False)
        set_provider("chatgpt", providers, False)
        expected = pools.PROVIDER_DISABLED
    elif cause == "no_key":
        monkeypatch.setattr(
            "django_ergo.conversation.engines.claude_code.claude_installed",
            lambda command="": False,
        )
        monkeypatch.setattr(
            "django_ergo.conversation.engines.codex_cli.codex_installed",
            lambda command="": False,
        )
        expected = pools.UNAVAILABLE
    else:
        session.metadata = {pools.ORIGIN_KEY: ["personal"]}
        session.save()
        expected = pools.PRIVACY_DENIED
    with pytest.raises(PolicyError) as refused:
        bot.route(session)
    assert getattr(refused.value, "code", pools.PRIVACY_DENIED) == expected
    assert not RouteDecision.objects.filter(outcome="selected").exists()


@pytest.mark.django_db
def test_budget_denial_on_the_active_pool_never_rolls_back(user):
    _, found = registry_of()
    bot = found["devbox"]
    providers = bot.providers
    set_pref(user.pk, providers, active_pool="paid", auto_rollover=True)
    set_state("paid", providers, allow_auto_entry=True)
    own = pools.snapshot(providers, user.pk).get("paid").grant
    pools.revoke_grant(providers, own.pk, by_id=user.pk)
    with pytest.raises(pools.PoolRefusal) as refused:
        bot.route(chat(bot, user))
    assert refused.value.code == pools.BUDGET_REQUIRED


# -- pins --------------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_pin_outside_the_active_pool_is_refused_not_substituted(user):
    _, found = registry_of()
    bot = found["devbox"]
    session = chat(bot, user)
    with pytest.raises(pools.PoolRefusal) as refused:
        bot.pick_model(session, "openai/gpt-6.1-sol")
    assert refused.value.code == pools.PIN_OUT_OF_POOL
    session.refresh_from_db()
    assert session.model == ""
    # A pin stored some other way fails at the turn, never swaps models.
    session.model = "openai/gpt-6.1-sol"
    session.save()
    with pytest.raises(pools.PoolRefusal, match="isn't in the active pool"):
        bot.route(session)
    bot.pick_model(session, "claude/claude-sonnet-5-5")
    assert bot.route(session) == ""


# -- metered budgets -----------------------------------------------------------------------


@pytest.mark.django_db
def test_unpriced_metered_model_is_not_usable(user):
    _, found = registry_of()
    bot = found["devbox"]
    set_pref(user.pk, bot.providers, active_pool="paid")
    session = chat(bot, user)
    with pytest.raises(pools.PoolRefusal) as refused:
        bot.pick_model(session, "openai/gpt-6-luna")
    assert refused.value.code == pools.PRICE_UNKNOWN


@pytest.mark.django_db
def test_budget_expiry_makes_the_pool_ineligible(user):
    _, found = registry_of(POOLS.replace("expires: 2099-12-31", "expires: 2000-01-01"))
    bot = found["devbox"]
    providers = bot.providers
    set_pref(user.pk, providers, active_pool="paid")
    with pytest.raises(pools.PoolRefusal) as refused:
        bot.route(chat(bot, user))
    assert refused.value.code == pools.BUDGET_EXPIRED


@pytest.mark.django_db(transaction=True)
async def test_metered_engine_reserves_before_each_send_and_prices_after(monkeypatch):
    from asgiref.sync import sync_to_async

    _, found = registry_of()
    bot = found["devbox"]
    owner = await User.objects.acreate(username="lee")
    await sync_to_async(pools.update_preference)(
        owner.pk, bot.providers, version=0, active_pool="paid"
    )
    session = await ConversationSession.objects.acreate(
        user=owner,
        bot_name="devbox",
        engine_type="openai",
        transport_type="api",
        model="openai/gpt-6.1-sol",
    )
    await sync_to_async(bot.route)(session)
    engine = bot.make_engine(session)
    assert engine.spend_guard is not None
    sent = []

    async def send(kwargs):
        sent.append(dict(kwargs))
        return SimpleNamespace(
            id="r1",
            usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=100),
        )

    monkeypatch.setattr(engine, "_send_completion", send)
    await engine._create_completion(
        {"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert sent[0]["max_completion_tokens"] == 16_000  # bounded output
    row = await InferenceRequest.objects.aget(billing_mode="metered")
    assert row.cost_status == "priced"
    assert row.cost_usd == Decimal("0.002")  # 1000 * $1/M + 100 * $10/M
    assert row.billing_mode == "metered"
    # A model the guard wasn't granted is refused before any send.
    with pytest.raises(spend_guard.SpendDenied):
        await engine._create_completion({"model": "gpt-6-luna", "messages": []})
    assert len(sent) == 1


@pytest.mark.django_db
def test_metered_route_without_a_pool_decision_cannot_send(user):
    _, found = registry_of()
    bot = found["devbox"]
    engine = bot.make_engine(model="openai/gpt-6.1-sol")
    assert engine.spend_guard.deny_reason
    import asyncio

    call = engine.spend_guard.call(
        {"model": "gpt-6.1-sol"}, "max_completion_tokens", lambda: None
    )
    with pytest.raises(spend_guard.SpendDenied):
        asyncio.run(call)


@pytest.mark.django_db(transaction=True)
def test_reservations_are_serialized_under_concurrency():
    if connection.vendor != "postgresql":
        pytest.skip("advisory locks need PostgreSQL")
    owner = User.objects.create(username="lee")
    providers = parse_providers(POOLS)
    held = grant(providers, owner, budget="1.00")
    results = []
    barrier = threading.Barrier(4)

    def reserve():
        from django.db import connections

        barrier.wait()
        try:
            spend_guard.reserve(
                grant_id=held.pk,
                provider="openai",
                model="gpt-6.1-sol",
                amount=Decimal("0.40"),
                user_id=owner.pk,
                fields={"bot_name": "devbox", "selector": "openai/gpt-6.1-sol"},
            )
            results.append("ok")
        except spend_guard.SpendDenied:
            results.append("denied")
        finally:
            connections.close_all()

    threads = [threading.Thread(target=reserve) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["denied", "denied", "ok", "ok"]
    spent, reserved = spend_guard.spent_and_reserved(held.pk)
    assert (spent, reserved) == (Decimal(0), Decimal("0.80"))


@pytest.mark.django_db
def test_guard_rereads_the_grant_at_send_time(user):
    from django_ergo.conversation.models import SpendGrant

    providers = parse_providers(POOLS)

    def send(held, who=user, model="gpt-6.1-sol"):
        return spend_guard.reserve(
            grant_id=held.pk,
            provider="openai",
            model=model,
            amount=Decimal("0.01"),
            user_id=who.pk,
            fields={"bot_name": "x", "selector": "x"},
        )

    small = grant(providers, user, budget="0.0001")
    with pytest.raises(spend_guard.SpendDenied, match="at its budget"):
        send(small)
    big = grant(providers, user, budget="10", routes=["openai/gpt-6.1-sol"])
    # (it replaced and revoked the small one)
    with pytest.raises(spend_guard.SpendDenied, match="revoked"):
        send(small)
    stranger = User.objects.create(username="stranger")
    with pytest.raises(spend_guard.SpendDenied, match="another user"):
        send(big, who=stranger)
    with pytest.raises(spend_guard.SpendDenied, match="doesn't cover"):
        send(big, model="gpt-6-luna")
    set_state("paid", providers, enabled=False)
    with pytest.raises(spend_guard.SpendDenied, match="switched off"):
        send(big)
    set_state("paid", providers, enabled=True)
    send(big)
    expired = SpendGrant.objects.create(
        pool="paid",
        owner=user,
        grantee=user,
        routes=["openai/gpt-6.1-sol"],
        budget_usd=10,
        max_output_tokens=100,
        valid_from=timezone.now() - dt.timedelta(days=2),
        expires_at=timezone.now() - dt.timedelta(seconds=1),
    )
    with pytest.raises(spend_guard.SpendDenied, match="expired"):
        send(expired)
    big.budget_usd = 100
    with pytest.raises(ValueError, match="immutable"):
        big.save()


# -- provider switches -----------------------------------------------------------------------


@pytest.mark.django_db
def test_switched_off_provider_is_excluded_everywhere(user):
    _, found = registry_of()
    bot = found["devbox"]
    providers = bot.providers
    set_provider("claude", providers, False)
    session = chat(bot, user)
    # Routing and retries.
    assert bot.route(session) == "chatgpt/gpt-6.1-sol"
    # Listing (the picker) shows it unavailable.
    listed = {
        m["id"]: m for p in pools.listing(bot, session)["pools"] for m in p["models"]
    }
    assert listed["claude/claude-opus-5-5"]["available"] is False
    assert "claude/claude-opus-5-5" not in pools.chat_models(bot, session)
    # Pins.
    with pytest.raises(pools.PoolRefusal) as refused:
        bot.pick_model(session, "claude/claude-opus-5-5")
    assert refused.value.code == pools.PROVIDER_DISABLED
    # New coding workers.
    decision = pools.select_agent(bot, session, tier="medium")
    assert decision.choice.agent == "codex"
    # Legacy deployments honour the switch too.
    _, legacy = registry_of(LEGACY)
    set_provider("claude", legacy["devbox"].providers, False)
    with pytest.raises(ValueError, match="No provider"):
        routing.pick_model(legacy["devbox"].providers, "medium")


# -- coding agents ----------------------------------------------------------------------------


@pytest.mark.django_db
def test_coding_inherits_the_active_pool(user):
    _, found = registry_of()
    bot = found["devbox"]
    session = chat(bot, user)
    decision = pools.select_agent(bot, session, tier="medium")
    assert decision.choice.agent in ("claude", "codex")
    assert decision.billing == "subscription"
    set_chat(session, "paid", bot.providers)
    # Metered coding is refused unless a manager confines the process AND
    # enforces a total spend bound; env/flag confinement alone isn't enough.
    for confinable in (False, True):
        with pytest.raises(pools.PoolRefusal) as refused:
            pools.select_agent(bot, session, tier="medium", confinable=confinable)
        assert refused.value.code == pools.UNCONFINABLE
        assert "unsupported" in str(refused.value)
    assert not InferenceRequest.objects.exists()  # nothing reserved, nothing launched
    # Never another provider instead: the subscription routes aren't offered.
    assert all(
        r["route"].startswith(("openai/", "anthropic/")) for r in refused.value.rejected
    )


@pytest.mark.django_db
def test_a_manager_with_a_real_spend_bound_would_reserve_per_launch(user):
    """The eligibility path for a future manager that can enforce both."""
    _, found = registry_of()
    bot = found["devbox"]
    session = chat(bot, user, pool="paid")
    decision = pools.select_agent(
        bot, session, tier="medium", confinable=True, spend_bounded=True
    )
    assert decision.metered
    confined = pools.confinement(bot, decision)
    assert confined["model"] == decision.choice.model
    assert confined["key_env"] in ("TEST_POOL_OPENAI_KEY", "TEST_POOL_ANTHROPIC_KEY")
    row = InferenceRequest.objects.get(pool=f"grant:{decision.grant_id}")
    assert row.reserved_usd == Decimal(2)
    assert row.purpose == "coding_launch"


def test_no_shipped_agent_manager_launches_metered_coding():
    from django_ergo.bots.agents import AgentManager
    from django_ergo.plugins.local_shell import LocalShellAgents
    from django_ergo.plugins.orca import OrcaAgents

    for manager in (AgentManager, LocalShellAgents, OrcaAgents):
        assert not (manager.metered_confinement and manager.metered_spend_bound)
    assert LocalShellAgents.metered_confinement is True  # defense in depth only
    assert OrcaAgents.metered_confinement is False


@pytest.mark.django_db
def test_agents_start_refuses_metered_coding_before_any_launch(user, monkeypatch):
    from django_ergo.bots import agents

    _, found = registry_of()
    bot = found["devbox"]
    session = chat(bot, user, pool="paid")
    launched = []

    class Manager(agents.AgentManager):
        name = "local"
        agents = ("claude", "codex")
        requires_approval = False
        metered_confinement = True  # confines, but can't bound spend

        def start(self, ctx, spec):
            launched.append(spec)
            return {}

    monkeypatch.setattr(agents, "manager_for", lambda *a, **k: Manager())
    ctx = SimpleNamespace(bot=bot, session=session, user=user, workers=None)
    spec = agents.AgentSpec(brief="fix it", workspace="repo", tier="medium")
    with pytest.raises(pools.PoolRefusal) as refused:
        agents.start.__wrapped__(ctx, spec) if hasattr(
            agents.start, "__wrapped__"
        ) else agents.start(ctx, spec)
    assert refused.value.code == pools.UNCONFINABLE
    assert launched == []
    assert not InferenceRequest.objects.exists()


@pytest.mark.django_db
def test_explicit_coding_model_must_be_in_the_pool(user):
    _, found = registry_of()
    bot = found["devbox"]
    session = chat(bot, user)
    decision = pools.select_agent(bot, session, model="claude-opus-5-5", agent="claude")
    assert decision.choice.provider == "claude"
    set_chat(session, "paid", bot.providers)
    with pytest.raises(pools.PoolRefusal) as refused:
        pools.select_agent(
            bot, session, model="claude-sonnet-5-5", confinable=True, spend_bounded=True
        )
    assert refused.value.code == pools.PIN_OUT_OF_POOL


@pytest.mark.django_db
def test_omp_is_refused_on_a_metered_route(user):
    text = POOLS.replace(
        "    tiers: {medium: [openai/gpt-6.1-sol, anthropic/claude-opus-5-5]}",
        "    tiers: {medium: [openai/gpt-6.1-sol, anthropic/claude-opus-5-5]}\n"
        "    agents: {medium: [{agent: omp, model: gpt-6.1-sol, provider: openai}]}",
    )
    _, found = registry_of(text)
    bot = found["devbox"]
    session = chat(bot, user, pool="paid")
    with pytest.raises(pools.PoolRefusal) as refused:
        pools.select_agent(
            bot, session, tier="medium", confinable=True, spend_bounded=True
        )
    assert refused.value.code == pools.UNCONFINABLE
    assert "omp" in str(refused.value)


def test_local_shell_confines_env_and_flags(tmp_path, monkeypatch):
    from django_ergo.plugins.local_shell import LocalShellPlugin

    monkeypatch.setenv("TEST_POOL_OPENAI_KEY", "sk-selected")
    plugin = LocalShellPlugin(None, {})
    plugin.on_load()
    env = {
        "PATH": "/bin",
        "HOME": "/home/dev",
        "ANTHROPIC_API_KEY": "sk-other",
        "GEMINI_API_KEY": "g",
        "XAI_API_KEY": "x",
        "OPENAI_BASE_URL": "https://elsewhere",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "AWS_SECRET_ACCESS_KEY": "aws",
        "GOOGLE_APPLICATION_CREDENTIALS": "/creds.json",
        "AZURE_OPENAI_API_KEY": "az",
        "OPENROUTER_API_KEY": "or",
        "ANTHROPIC_AUTH_TOKEN": "tok",
        "GITHUB_TOKEN": "keep",
    }
    confined = {
        "agent": "codex",
        "model": "gpt-6.1-sol",
        "key_env": "TEST_POOL_OPENAI_KEY",
    }
    out = plugin.confined_env(env, confined, tmp_path)
    assert out["OPENAI_API_KEY"] == out["CODEX_API_KEY"] == "sk-selected"
    for gone in set(env) - {"PATH", "HOME", "GITHUB_TOKEN"}:
        assert gone not in out, gone
    assert out["GITHUB_TOKEN"] == "keep"
    assert out["HOME"] == str(tmp_path / ".ergo" / "home")
    argv = plugin.argv("codex", "do it", model="", number=1)
    argv = plugin._with_confinement(argv, "codex", confined)
    assert argv[:6] == [
        "codex",
        "exec",
        "-c",
        'model_provider="openai"',
        "--model",
        "gpt-6.1-sol",
    ]
    claude = {
        "agent": "claude",
        "model": "claude-opus-5-5",
        "key_env": "TEST_POOL_ANTHROPIC_KEY",
    }
    argv = plugin._with_confinement(
        plugin.argv("claude", "x", model="", number=1), "claude", claude
    )
    assert argv[1:3] == ["--model", "claude-opus-5-5"]
    assert "--fallback-model" not in argv
    monkeypatch.delenv("TEST_POOL_OPENAI_KEY")
    from django_ergo.plugins.local_shell import LocalShellError

    with pytest.raises(LocalShellError, match="isn't set"):
        plugin.confined_env(env, confined, tmp_path)
    with pytest.raises(LocalShellError):
        plugin.confined_env(env, {**confined, "agent": "omp"}, tmp_path)


@pytest.mark.django_db
def test_resumed_confined_launch_revalidates_and_reserves_again(user):
    _, found = registry_of()
    bot = found["devbox"]
    session = chat(bot, user, pool="paid")
    confined = {
        "pool": "paid",
        "provider": "openai",
        "model": "gpt-6.1-sol",
        "agent": "codex",
        "key_env": "TEST_POOL_OPENAI_KEY",
    }
    pools.revalidate_launch(bot, session, confined)
    assert InferenceRequest.objects.filter(purpose="coding_launch").count() == 1
    set_provider("openai", bot.providers, False)
    with pytest.raises(pools.PoolRefusal) as refused:
        pools.revalidate_launch(bot, session, confined)
    assert refused.value.code == pools.PROVIDER_DISABLED


# -- privacy -------------------------------------------------------------------------------------


def nested(text=POOLS):
    """personal with a nested sub-bot, plus devbox."""
    registry = BotRegistry()
    registry.providers = parse_providers(text)
    personal = Bot(
        BotDefinition.from_dict(
            {"name": "personal", "engine": {"config": {"model": "personal"}}}
        )
    )
    sub = Bot(
        BotDefinition.from_dict(
            {"name": "notes", "engine": {"config": {"model": "personal"}}}
        )
    )
    devbox = Bot(BotDefinition.from_dict({"name": "devbox"}))
    for bot in (personal, sub, devbox):
        registry.add(bot)
    sub.parent_name = "personal"
    return registry, personal, sub, devbox


def test_nested_bot_inherits_its_parents_private_pool_and_policy():
    _, personal, sub, devbox = nested()
    assert sub.private_pool is personal.private_pool
    assert pools.privacy_for(sub).names == ("personal",)
    assert sub.engine_spec().engine_type == "fireworks"
    assert not pools.privacy_for(devbox).restricted
    # A nested bot can't escape to a public model.
    with pytest.raises(PolicyError):
        sub.engine_spec(model="claude/claude-opus-5-5")


@pytest.mark.django_db
def test_switching_user_pools_never_widens_a_restricted_bot(user):
    _, personal, _sub, _devbox = nested()
    set_pref(user.pk, personal.providers, active_pool="paid")
    session = chat(personal, user)
    decision = pools.select_route(personal, session, ref="personal")
    assert decision.mode == "private"
    with pytest.raises(pools.PoolRefusal) as refused:
        pools.select_route(personal, session, ref="openai/gpt-6.1-sol")
    assert refused.value.code == pools.PRIVACY_DENIED
    with pytest.raises(PolicyError):
        pools.select_agent(
            personal, session, tier="medium", confinable=True, spend_bounded=True
        )
    listing = pools.listing(personal, session)
    assert listing["privacy"]["restricted"]
    assert [m["id"] for p in listing["pools"] for m in p["models"]] == [
        "personal/heavy"
    ]


@pytest.mark.django_db
def test_delegated_message_from_a_restricted_origin_is_refused(user):
    from django_ergo.bots import messaging

    registry, personal, sub, devbox = nested()
    sender = chat(personal, user)
    recipient = chat(devbox, user)
    with pytest.raises(PolicyError, match="isn't one of them"):
        messaging.send(sender, recipient, "summarize my notes", registry=registry)
    # A bot inside the policy receives it, carrying the origin.
    target = chat(sub, user)
    message = messaging.send(sender, target, "file this", registry=registry)
    assert message.metadata.get(pools.ORIGIN_KEY) in (
        None,
        [],
    )  # sub's own policy covers it


def test_turn_origin_restricts_titles_compaction_and_browser(monkeypatch):
    _, found = registry_of()
    bot = found["devbox"]
    session = SimpleNamespace(
        pk=None,
        metadata={pools.ORIGIN_KEY: ["personal"]},
        model="",
        user_id=None,
        bot_name="devbox",
    )
    with pytest.raises(PolicyError):
        bot.make_ancillary_engine(
            session, purpose="new_thread_metadata", use_session=False
        )
    with pytest.raises(PolicyError):
        bot.make_ancillary_engine(session, purpose="compaction")
    with pytest.raises(PolicyError):
        bot.engine_spec(session)
    from django_ergo.plugins.browser import BrowserPlugin

    plugin = BrowserPlugin(bot, {})
    plugin.on_load()
    with pytest.raises(PolicyError, match="Privacy policy"):
        plugin.task_model(session)
    with pools.origin_scope(["personal"]), pytest.raises(PolicyError):
        bot.engine_spec(None)


def test_background_tasks_and_workers_carry_the_origin(monkeypatch):
    from django_ergo.bots import background

    seen = []
    bot = SimpleNamespace(
        name="devbox", tasks={"job": lambda: seen.append(pools.current_origin())}
    )
    monkeypatch.setattr(background, "find_bot", lambda name, registry=None: bot)
    captured = {}
    monkeypatch.setattr(
        background.api_settings,
        "BOT_TASK_RUNNER",
        lambda bot_name, name, args, kwargs: captured.update(kwargs)
        or SimpleNamespace(),
    )
    tasks = background.BotTasks(SimpleNamespace(name="devbox", tasks={"job": None}))
    with pools.origin_scope(["personal"]):
        tasks.start("job")
    assert captured[background.PRIVACY_KWARG] == ["personal"]
    background.execute.__wrapped__("devbox", "job", [], dict(captured)) if hasattr(
        background.execute, "__wrapped__"
    ) else background.execute("devbox", "job", [], dict(captured))
    assert seen == [("personal",)]


@pytest.mark.django_db
def test_history_of_a_restricted_origin_is_unreadable_to_unrestricted_bots(user):
    _, personal, sub, devbox = nested()
    private_chat = chat(personal, user)
    tainted = chat(devbox, user, **{pools.ORIGIN_KEY: ["personal"]})
    plain = chat(devbox, user)
    assert not privacy_readable(devbox, private_chat)
    assert not privacy_readable(devbox, tainted)
    assert privacy_readable(devbox, plain)
    assert privacy_readable(personal, private_chat)
    assert privacy_readable(sub, private_chat)
    sources = {s.session.pk for s in devbox.history_sources(plain)}
    assert tainted.pk not in sources and plain.pk in sources


def test_web_search_refuses_llm_backends_for_restricted_origins(monkeypatch):
    from django_ergo.plugins.web_search import WebSearchPlugin

    _, personal, _sub, devbox = nested()
    plugin = WebSearchPlugin(personal, {"backend": "claude_code"})
    plugin.on_load()
    monkeypatch.setattr(
        plugin, "_search_claude_code", lambda q, c: pytest.fail("LLM search ran")
    )
    assert "doesn't allow LLM search" in plugin.search("my notes")
    # A non-LLM backend the policy allows is used instead.
    text = POOLS + "privacy:\n  personal:\n    web_search: [brave]\n"
    _, personal, _sub, _devbox = nested(text)
    plugin = WebSearchPlugin(personal, {"backend": "claude_code"})
    plugin.on_load()
    monkeypatch.setenv("BRAVE_API_KEY", "b")
    monkeypatch.setattr(plugin, "_search_brave", lambda q, c: "brave results")
    assert plugin.search("my notes") == "brave results"
    public = WebSearchPlugin(devbox, {"backend": "claude_code"})
    public.on_load()
    monkeypatch.setattr(public, "_search_claude_code", lambda q, c: "claude results")
    assert public.search("x") == "claude results"


def test_video_analysis_is_refused_for_restricted_origins():
    from django_ergo.plugins.attachments import AttachmentsPlugin

    _, personal, _sub, _devbox = nested()
    plugin = AttachmentsPlugin(personal, {})
    plugin.on_load()
    ctx = SimpleNamespace(
        bot=personal, session=None, secret=lambda name: pytest.fail("key read")
    )
    row = SimpleNamespace(filename="clip.mp4")
    assert "doesn't allow" in plugin.watch(ctx, row, "what happens?")


# -- listing parity ---------------------------------------------------------------------------------


@pytest.mark.django_db
def test_listing_matches_what_selection_accepts(user):
    _, found = registry_of()
    bot = found["devbox"]
    set_provider("chatgpt", bot.providers, False)
    for active in ("subs", "paid"):
        session = chat(bot, user, pool=active)
        view = pools.listing(bot, session)
        assert view["active"] == active
        pool = next(p for p in view["pools"] if p["name"] == active)
        for model in pool["models"]:
            if model["available"]:
                pools.select_route(bot, session, ref=model["id"], record=False)
            else:
                with pytest.raises(pools.PoolRefusal):
                    pools.select_route(bot, session, ref=model["id"], record=False)


def test_a_turn_scope_replaces_the_ambient_origin():
    with pools.origin_scope(["personal"]):
        assert pools.current_origin() == ("personal",)
        with pools.origin_scope([], replace=True):
            assert pools.current_origin() == ()  # an inline delivery starts clean
        with pools.origin_scope(["work"]):
            assert pools.current_origin() == ("personal", "work")
    assert pools.current_origin() == ()
