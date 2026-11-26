"""Acceptance tests for the model pools re-review (#404 at 7fe9c83), N1-N5.

The reviewer's reproducers, inverted: each asserts the fixed behavior. No
network or paid inference: providers and usage are synthetic.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model

from django_ergo.bots import pools
from django_ergo.conversation import spend_guard
from django_ergo.pricing import ExactPrice
from tests.test_model_pools import POOLS
from tests.test_model_pools import chat
from tests.test_model_pools import environment  # noqa: F401 -- fake keys and CLIs
from tests.test_model_pools import grant
from tests.test_model_pools import registry_of

User = get_user_model()


def _paid_grant(text, user_id):
    _, bots = registry_of(text)
    return pools.snapshot(bots["devbox"].providers, user_id).get("paid").grant


# -- N1: providers.yaml changes don't narrow the owner's config grant ----------


@pytest.mark.xfail(strict=True, reason="N1 config grant accounting remains unresolved")
@pytest.mark.django_db
def test_rr_config_revert_keeps_the_wider_intermediate_grant():
    """A (budget 10) -> B (budget 1000) -> A again: the owner still holds B's
    $1000 grant, because A's fingerprint finds its old (revoked) row and no
    new grant is created, while the newest unrevoked grant is B's."""
    lee = User.objects.create(username="lee")
    a = _paid_grant(POOLS, lee.pk)
    assert a.budget_usd == 10
    wide = POOLS.replace("budget_usd: 10", "budget_usd: 1000")
    b = _paid_grant(wide, lee.pk)
    assert b.budget_usd == 1000
    a.refresh_from_db()
    assert a.revoked_at is not None
    reverted = _paid_grant(POOLS, lee.pk)  # config says 10 again
    assert reverted.budget_usd == 10
    assert reverted.pk != b.pk


@pytest.mark.xfail(strict=True, reason="N1 config grant accounting remains unresolved")
@pytest.mark.django_db
def test_rr_removing_the_config_budget_keeps_spend_authority():
    """Deleting budget_usd/expires (the owner's own grant) from providers.yaml
    leaves the old config grant active: nothing revokes it."""
    lee = User.objects.create(username="lee")
    old = _paid_grant(POOLS, lee.pk)
    no_budget = POOLS.replace("    budget_usd: 10\n", "").replace(
        "    per_launch_usd: 2\n", ""
    )
    _, bots = registry_of(no_budget)
    bot = bots["devbox"]
    assert bot.providers.pools.get("paid").budget_usd is None
    view = pools.snapshot(bot.providers, lee.pk).get("paid")
    assert view.grant is None
    assert view.grant_problem() is not None
    session = chat(bot, lee)
    pools.update_preference(lee.pk, bot.providers, version=0, active_pool="paid")
    decision = pools.select_route(bot, session, ref="auto/medium")
    assert decision.metered and decision.grant_id == old.pk


@pytest.mark.xfail(strict=True, reason="N1 config grant accounting remains unresolved")
@pytest.mark.django_db
def test_rr_previous_owner_keeps_the_pool_after_ownership_moves():
    """owner: lee -> owner: bob. lee's config grant (spending what is now
    bob's authority) stays active, so lee still sees and spends on the pool."""
    lee = User.objects.create(username="lee")
    User.objects.create(username="bob")
    old = _paid_grant(POOLS, lee.pk)
    moved = POOLS.replace("    owner: lee\n", "    owner: bob\n")
    _, bots = registry_of(moved)
    bot = bots["devbox"]
    view = pools.snapshot(bot.providers, lee.pk).get("paid")
    assert view is None
    session = chat(bot, lee)
    pools.update_preference(lee.pk, bot.providers, version=0, active_pool="paid")
    decision = pools.select_route(bot, session, ref="auto/medium")
    assert decision.metered and decision.grant_id == old.pk


# -- N2: the first pool in order is entered without its entry approval -------


PAID_FIRST = POOLS.replace(
    "pools:\n  - name: subs\n    label: Subscriptions\n    routes: [claude, chatgpt, xai-oauth]\n",
    "pools:\n",
).replace(
    "private_pools:",
    "  - name: subs\n    label: Subscriptions\n    routes: [claude, chatgpt, xai-oauth]\n"
    "private_pools:",
)


@pytest.mark.django_db
def test_rr_grantee_is_not_auto_entered_into_a_first_paid_pool():
    """N2: a grantee with no saved order and allow_auto_entry false (the
    metered default) isn't entered automatically into the sponsor's paid
    pool just because it is first in config order."""
    _, bots = registry_of(PAID_FIRST)
    bot = bots["devbox"]
    assert [p.name for p in bot.providers.pools.pools] == ["paid", "subs"]
    User.objects.create(username="lee")
    friend = User.objects.create(username="friend")
    grant(bot.providers, friend, budget="3")
    snap = pools.snapshot(bot.providers, friend.pk)
    assert not snap.get("paid").allow_auto_entry
    with pytest.raises(pools.PoolRefusal):
        pools.select_route(bot, chat(bot, friend), ref="auto/medium")


# -- N3: a user can't save an order for pools they can't see ------------------


@pytest.mark.django_db
def test_rr_reorder_lists_only_visible_pools():
    """N3: a user orders the pools they can see; hidden pools keep their
    places and naming one is refused."""
    _, bots = registry_of()
    bot = bots["devbox"]
    User.objects.create(username="lee")
    stranger = User.objects.create(username="stranger")
    visible = [p["name"] for p in pools.listing(bot, chat(bot, stranger))["pools"]]
    assert visible == ["subs"]
    pools.update_preference(stranger.pk, bot.providers, version=0, order=visible)
    with pytest.raises(ValueError, match="visible pool"):
        pools.update_preference(
            stranger.pk, bot.providers, version=1, order=["subs", "paid"]
        )


# -- N4: Anthropic adapter contract gaps --------------------------------------


def _anthropic_price():
    return ExactPrice(
        provider="anthropic",
        model="claude-opus-5-5",
        input=Decimal(5),
        cached=Decimal("0.5"),
        output=Decimal(25),
        cache_write=Decimal("6.25"),  # the 5-minute write rate
    )


def test_rr_anthropic_one_hour_cache_writes_are_refused():
    """N4: ttl 1h writes bill 2x input, not the single (5-minute)
    cache_write rate, so the adapter refuses them before sending."""
    adapter = spend_guard.AnthropicMessagesAdapter()
    kwargs = {
        "model": "claude-opus-5-5",
        "max_tokens": 10,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "x" * 1000,
                        "cache_control": {"type": "ephemeral", "ttl": "1h"},
                    }
                ],
            }
        ],
    }
    with pytest.raises(spend_guard.UnsupportedRequest):
        adapter.bound(kwargs, 16_000)


def test_rr_anthropic_adapter_refuses_non_dict_content_blocks():
    """N5: a block that isn't a dict (an SDK image object) is refused."""
    adapter = spend_guard.AnthropicMessagesAdapter()
    image = SimpleNamespace(type="image", source={"type": "url", "url": "https://x"})
    kwargs = {
        "model": "claude-opus-5-5",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": [image]}],
    }
    with pytest.raises(spend_guard.UnsupportedRequest):
        adapter.bound(kwargs, 16_000)


def test_rr_anthropic_one_hour_cache_usage_keeps_the_reservation():
    """N4: usage reporting 1-hour cache writes can't be priced at the
    5-minute rate, so it is treated as unknown (the hold stays)."""
    adapter = spend_guard.AnthropicMessagesAdapter()

    def response(one_hour):
        return SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=5,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=100,
                cache_creation=SimpleNamespace(
                    ephemeral_5m_input_tokens=100 - one_hour,
                    ephemeral_1h_input_tokens=one_hour,
                ),
            )
        )

    assert adapter.usage(response(0)) == spend_guard.Usage(10, 0, 100, 5)
    assert adapter.usage(response(100)) is None
    five_minutes = {"type": "ephemeral"}
    kwargs = {
        "model": "claude-opus-5-5",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "x", "cache_control": five_minutes}
                ],
            }
        ],
    }
    assert adapter.bound(kwargs, 100).cache_write


@pytest.mark.django_db
def test_rr_reorder_keeps_hidden_pools_in_place():
    """N3: reordering keeps a pool the user can't see where it was, and
    never writes a hidden pool's name into their order."""
    _, bots = registry_of()
    bot = bots["devbox"]
    lee = User.objects.create(username="lee")
    providers = bot.providers
    pools.update_preference(lee.pk, providers, version=0, order=["paid", "subs"])
    stored = pools.update_preference(
        lee.pk, providers, version=1, order=["subs", "paid"]
    )
    assert stored.order == ["subs", "paid"]
    stranger = User.objects.create(username="stranger")
    pools.update_preference(stranger.pk, providers, version=0, order=["subs"])
    assert pools.preference(stranger.pk).order == ["subs"]  # "paid" isn't theirs
    # A grantee who ordered paid first, then lost the grant: paid keeps its slot.
    friend = User.objects.create(username="friend")
    held = grant(providers, friend, budget="3")
    pools.update_preference(friend.pk, providers, version=0, order=["paid", "subs"])
    pools.revoke_grant(providers, held.pk, by_id=lee.pk)
    pools.update_preference(friend.pk, providers, version=1, order=["subs"])
    assert pools.preference(friend.pk).order == ["paid", "subs"]


# -- Positive control: the 0059 migration from a main-0058 DB, and back --------


@pytest.mark.django_db(transaction=True)
def test_rr_migration_0059_upgrades_from_main_0058_and_reverses():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    from django_ergo.conversation.models import AgentLaunch

    tables = {
        "django_ergo_spendgrant",
        "django_ergo_modelpoolstate",
        "django_ergo_modelpoolpreference",
        "django_ergo_routedecision",
    }

    def present():
        return tables & set(connection.introspection.table_names())

    executor = MigrationExecutor(connection)
    leaf = ("django_ergo", "0059_model_pools")
    main = ("django_ergo", "0058_agent_launch_intent")
    expected = {
        m._meta.db_table
        for m in __import__("django.apps")
        .apps.apps.get_app_config("django_ergo")
        .get_models()
        if m.__module__.endswith("conversation.models")
        and m.__name__
        in ("SpendGrant", "ModelPoolState", "ModelPoolPreference", "RouteDecision")
    }
    tables.clear()
    tables.update(expected)
    assert len(tables) == 4 and present() == tables
    try:
        executor.migrate([main])  # reverse 0059 to main's leaf
        assert present() == set()
        assert AgentLaunch._meta.db_table in connection.introspection.table_names()
        executor = MigrationExecutor(connection)
        assert executor.migration_plan([leaf])  # 0059 pending on a main DB
        executor.migrate([leaf])
        assert present() == tables
    finally:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
