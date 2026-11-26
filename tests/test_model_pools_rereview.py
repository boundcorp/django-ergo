"""Acceptance tests for the model pools re-review (#404 at 7fe9c83), N1-N5.

The reviewer's reproducers, inverted: each asserts the fixed behavior. No
network or paid inference: providers and usage are synthetic.
"""

from __future__ import annotations

import datetime as dt
import threading
from decimal import Decimal
from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone

from django_ergo.bots import pools
from django_ergo.bots.providers import parse_providers
from django_ergo.conversation import spend_guard
from django_ergo.conversation.models import InferenceRequest
from django_ergo.pricing import ExactPrice
from tests.test_model_pools import POOLS
from tests.test_model_pools import chat
from tests.test_model_pools import environment  # noqa: F401 -- fake keys and CLIs
from tests.test_model_pools import grant
from tests.test_model_pools import registry_of

User = get_user_model()


# -- N1: one spending envelope per config-derived authority --------------------
#
# providers.yaml edits add revisions to the owner's envelope and removals or
# ownership moves revoke it; restoring continues the same cumulative ledger.
# Every send re-reads the current config under the envelope's lock and uses
# the intersection with the revision it was built or queued with.

PAID_BUDGET = "    budget_usd: 10\n"
NO_BUDGET = POOLS.replace(PAID_BUDGET, "").replace("    per_launch_usd: 2\n", "")


def _config(text):
    """A registry on ``text`` and its devbox bot."""
    registry, bots = registry_of(text)
    return registry, bots["devbox"]


def _own(providers, user):
    return pools.snapshot(providers, user.pk).get("paid")


def _hold(grant_row, user, amount, providers):
    """Reserve ``amount`` on the paid OpenAI route (a held, unpriced attempt)."""
    return spend_guard.reserve(
        grant_id=grant_row.pk,
        provider="openai",
        model="gpt-6.1-sol",
        amount=Decimal(amount),
        user_id=user.pk,
        fields={"bot_name": "devbox", "selector": "openai/gpt-6.1-sol"},
        current=pools.SendAuthority(providers),
    )


def _guard(bot, user):
    """A spend guard built the way a turn builds one, on the paid route."""
    session = chat(bot, user)
    pools.update_preference(
        user.pk,
        bot.providers,
        version=getattr(pools.preference(user.pk), "version", 0),
        active_pool="paid",
    )
    decision = pools.select_route(bot, session, ref="openai/gpt-6.1-sol")
    assert decision.metered
    return decision.guard(bot, session)


def _send(guard):
    async def send():
        return SimpleNamespace(id="mock", usage=None)  # usage unknown: hold kept

    return async_to_sync(guard.call)(
        {"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "hi"}]},
        "max_completion_tokens",
        send,
    )


@pytest.mark.django_db
def test_rr_config_revert_continues_the_same_envelope():
    """A ($10) -> B ($1000) -> A: the owner's authority is A's terms again,
    as a new revision of the same envelope; nothing spent or held under A
    is forgotten, and B's wider budget is gone."""
    lee = User.objects.create(username="lee")
    a = _own(parse_providers(POOLS), lee).grant
    _hold(a, lee, "6", parse_providers(POOLS))
    wide = POOLS.replace("budget_usd: 10", "budget_usd: 1000")
    b = _own(parse_providers(wide), lee).grant
    assert b.budget_usd == 1000 and b.envelope_id == a.envelope_id
    a.refresh_from_db()
    assert a.superseded_at is not None and a.revoked_at is None
    reverted = _own(parse_providers(POOLS), lee)
    assert reverted.grant.budget_usd == 10
    assert reverted.grant.pk not in (a.pk, b.pk)
    assert reverted.grant.envelope_id == a.envelope_id
    assert (reverted.spent_usd, reverted.reserved_usd) == (Decimal(0), Decimal(6))
    with pytest.raises(spend_guard.SpendDenied, match="at its budget"):
        _hold(reverted.grant, lee, "5", parse_providers(POOLS))
    _hold(reverted.grant, lee, "4", parse_providers(POOLS))
    # B's revision, held by a guard built under B, is held to A's terms now.
    with pytest.raises(spend_guard.SpendDenied, match="at its budget"):
        _hold(b, lee, "1", parse_providers(POOLS))


@pytest.mark.django_db
def test_rr_removing_the_config_budget_revokes_spend_authority():
    """Deleting budget_usd from providers.yaml revokes the owner's authority:
    selection refuses, and a guard built before the edit refuses its next
    send. Restoring the budget continues the same envelope, with the
    earlier hold still counted."""
    registry, bot = _config(POOLS)
    lee = User.objects.create(username="lee")
    old = _own(bot.providers, lee).grant
    guard = _guard(bot, lee)
    _send(guard)  # one attempt with unknown usage: its hold stays
    held = spend_guard.spent_and_reserved(old.pk)[1]
    assert held > 0
    registry.providers = parse_providers(NO_BUDGET)  # the edit, live
    with pytest.raises(spend_guard.SpendDenied, match="no longer sets a budget"):
        _send(guard)  # already built: the send re-reads the config
    old.refresh_from_db()
    assert old.revoked_at is not None
    view = _own(bot.providers, lee)
    assert view.grant is None and view.grant_problem() is not None
    with pytest.raises(pools.PoolRefusal):
        pools.select_route(bot, chat(bot, lee), ref="openai/gpt-6.1-sol")
    registry.providers = parse_providers(POOLS)  # restored
    restored = _own(bot.providers, lee)
    assert restored.grant.envelope_id == old.envelope_id
    assert restored.grant.pk != old.pk
    assert restored.reserved_usd == held  # the uncertain hold stays counted
    with pytest.raises(spend_guard.SpendDenied):
        _send(guard)  # the revoked revision never comes back


@pytest.mark.django_db
def test_rr_lowering_then_raising_the_budget_never_replenishes():
    """10 -> 5 -> 10 with $4 held: the cumulative total carries across every
    revision, so raising the budget again leaves $6, not $10."""
    lee = User.objects.create(username="lee")
    first = _own(parse_providers(POOLS), lee).grant
    _hold(first, lee, "4", parse_providers(POOLS))
    lower = POOLS.replace("budget_usd: 10", "budget_usd: 5")
    low = _own(parse_providers(lower), lee)
    assert low.grant.budget_usd == 5 and low.reserved_usd == 4
    with pytest.raises(spend_guard.SpendDenied, match="at its budget"):
        _hold(low.grant, lee, "2", parse_providers(lower))
    again = _own(parse_providers(POOLS), lee)
    assert again.grant.budget_usd == 10
    assert {first.envelope_id, low.grant.envelope_id} == {again.grant.envelope_id}
    with pytest.raises(spend_guard.SpendDenied, match="at its budget"):
        _hold(again.grant, lee, "6.01", parse_providers(POOLS))
    _hold(again.grant, lee, "6", parse_providers(POOLS))


@pytest.mark.django_db
def test_rr_ownership_move_revokes_the_previous_owners_authority():
    """owner: lee -> bob. lee no longer sees or spends on the pool (a guard
    built before the move refuses, and lee's config revisions are revoked);
    the grants lee issued stop working at once (suspended: they work only
    while lee owns the pool). Bob gets no money from the move itself: the
    pool's accounts are already funded (lee's envelope), so bob's authority
    waits for an admin to fund it separately, as his own envelope. Moving
    the pool back to lee continues lee's envelope."""
    registry, bot = _config(POOLS)
    lee = User.objects.create(username="lee")
    bob = User.objects.create(username="bob")
    friend = User.objects.create(username="friend")
    old = _own(bot.providers, lee).grant
    issued = grant(bot.providers, friend, budget="3")
    guard = _guard(bot, lee)
    friends = _guard(bot, friend)
    _send(guard)
    moved = POOLS.replace("    owner: lee\n", "    owner: bob\n")
    registry.providers = parse_providers(moved)
    for built in (guard, friends):
        with pytest.raises(spend_guard.SpendDenied, match="another owner"):
            _send(built)
    assert _own(bot.providers, lee) is None
    assert _own(bot.providers, friend) is None
    session = chat(bot, lee)
    with pytest.raises(pools.PoolRefusal):  # lee's pick of paid is stale now
        pools.select_route(bot, session, ref="openai/gpt-6.1-sol")
    with pytest.raises(PermissionError):
        pools.update_preference(lee.pk, bot.providers, version=1, active_pool="paid")
    old.refresh_from_db()
    assert old.revoked_at is not None
    waiting = _own(bot.providers, bob)
    assert waiting.grant is None
    assert "an admin must link" in waiting.grant_problem()[1]
    admin = User.objects.create(username="admin", is_superuser=True)
    pools.link_config_envelope(
        bot.providers, "paid", by_id=admin.pk, admin=True, fund=True
    )
    bobs = _own(bot.providers, bob).grant
    assert bobs.envelope_id != old.envelope_id
    assert spend_guard.spent_and_reserved(bobs.pk) == (Decimal(0), Decimal(0))
    registry.providers = parse_providers(POOLS)
    back = _own(bot.providers, lee)
    assert back.grant.envelope_id == old.envelope_id and back.reserved_usd > 0
    issued.refresh_from_db()
    assert issued.revoked_at is None
    assert _own(bot.providers, friend).grant.pk == issued.pk
    _send(friends)  # lee's own grant to friend, lee's pool again


@pytest.mark.django_db
def test_rr_a_stale_config_never_destroys_the_current_owners_grants():
    """A process still on the old config (owner lee) reads pools after the
    move to bob: it can't revoke the grants bob issued, and bob's process
    puts its own config back."""
    lee = User.objects.create(username="lee")
    bob = User.objects.create(username="bob")
    friend = User.objects.create(username="friend")
    stale = parse_providers(POOLS)
    _own(stale, lee)
    fresh = parse_providers(POOLS.replace("    owner: lee\n", "    owner: bob\n"))
    assert _own(fresh, bob).grant is None  # the accounts are lee's envelope's
    admin = User.objects.create(username="admin", is_superuser=True)
    pools.link_config_envelope(fresh, "paid", by_id=admin.pk, admin=True, fund=True)
    assert _own(fresh, bob).grant is not None
    bobs_grant = pools.issue_grant(
        fresh,
        "paid",
        issuer_id=bob.pk,
        grantee_id=friend.pk,
        budget_usd="3",
        expires_at=timezone.now() + dt.timedelta(days=3),
    )
    pools.snapshot(stale, lee.pk)  # the stale process
    bobs_grant.refresh_from_db()
    assert bobs_grant.revoked_at is None
    assert _own(fresh, friend).grant.pk == bobs_grant.pk
    _hold(bobs_grant, friend, "1", fresh)
    assert _own(fresh, lee) is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "  - name: paid\n    label: Paid API\n",
            "  - name: paid\n    label: Paid API\n    enabled: false\n",
        ),
        (
            "    api_key_env: TEST_POOL_OPENAI_KEY\n",
            "    api_key_env: TEST_POOL_OPENAI_KEY\n    enabled: false\n",
        ),
    ],
    ids=["pool", "provider"],
)
def test_rr_switching_off_in_providers_yaml_stops_a_built_guard(old, new):
    registry, bot = _config(POOLS)
    lee = User.objects.create(username="lee")
    guard = _guard(bot, lee)
    registry.providers = parse_providers(POOLS.replace(old, new))
    with pytest.raises(spend_guard.SpendDenied):
        _send(guard)
    assert not InferenceRequest.objects.exists()


@pytest.mark.django_db
def test_rr_revoked_issued_grant_refuses_an_already_built_guard():
    registry, bot = _config(POOLS)
    lee = User.objects.create(username="lee")
    friend = User.objects.create(username="friend")
    issued = grant(bot.providers, friend, budget="3")
    guard = _guard(bot, friend)
    assert guard.grant_id == issued.pk
    _send(guard)
    pools.revoke_grant(bot.providers, issued.pk, by_id=lee.pk)
    with pytest.raises(spend_guard.SpendDenied, match="revoked"):
        _send(guard)


@pytest.mark.django_db
def test_rr_a_person_revoking_config_terms_isnt_undone_by_sync():
    """The owner gives up their config revision: config sync doesn't mint
    it again, after a route edit or a money edit (even a narrowing one);
    only an admin's explicit restore does, on the same envelope."""
    lee = User.objects.create(username="lee")
    providers = parse_providers(POOLS)
    own = _own(providers, lee).grant
    pools.revoke_grant(providers, own.pk, by_id=lee.pk)
    assert _own(providers, lee).grant is None
    # A route or ceiling edit leaves the money terms alone: still revoked.
    more = POOLS.replace(
        "      - {name: claude-opus-5-5, price: {input: 5, output: 25}}\n",
        "      - {name: claude-opus-5-5, price: {input: 5, output: 25}}\n"
        "      - claude-haiku-5\n",
    )
    assert _own(parse_providers(more), lee).grant is None
    later = POOLS.replace("expires: 2099-12-31", "expires: 2099-06-30")
    assert _own(parse_providers(later), lee).grant is None
    lower = POOLS.replace("budget_usd: 10", "budget_usd: 5")
    assert _own(parse_providers(lower), lee).grant is None
    admin = User.objects.create(username="admin", is_superuser=True)
    pools.link_config_envelope(
        parse_providers(later), "paid", by_id=admin.pk, admin=True, restore=True
    )
    renewed = _own(parse_providers(later), lee).grant
    assert renewed.envelope_id == own.envelope_id


@pytest.mark.django_db
def test_rr_queued_work_intersects_its_revision_with_the_current_config():
    """A job captured revision A ($10, 16k output). The config then narrows
    to $5 and a 100-token ceiling: the job's sends use the narrower terms.
    Widening the config to $1000 never widens the job past A's $10."""
    registry, bot = _config(POOLS)
    lee = User.objects.create(username="lee")
    captured = _own(bot.providers, lee).grant
    narrow = POOLS.replace("budget_usd: 10", "budget_usd: 5").replace(
        "    per_launch_usd: 2\n", "    per_launch_usd: 2\n    max_output_tokens: 100\n"
    )
    registry.providers = parse_providers(narrow)
    terms = spend_guard.authorize(
        grant_id=captured.pk, user_id=lee.pk, current=pools.SendAuthority(bot)
    )
    assert (terms.budget_usd, terms.max_output_tokens) == (5, 100)
    with pytest.raises(spend_guard.SpendDenied, match="at its budget"):
        _hold(captured, lee, "6", registry.providers)
    registry.providers = parse_providers(
        POOLS.replace("budget_usd: 10", "budget_usd: 1000")
    )
    terms = spend_guard.authorize(
        grant_id=captured.pk, user_id=lee.pk, current=pools.SendAuthority(bot)
    )
    assert terms.budget_usd == 10
    with pytest.raises(spend_guard.SpendDenied, match="at its budget"):
        _hold(captured, lee, "11", registry.providers)


@pytest.mark.django_db
def test_rr_a_guard_reads_the_config_of_the_reloaded_registry():
    """Ergonaut reloads bot files into a new registry; a guard built from a
    bot of the old one follows ``successor`` to the current config."""
    registry, bot = _config(POOLS)
    lee = User.objects.create(username="lee")
    guard = _guard(bot, lee)
    reloaded, _ = registry_of(NO_BUDGET)
    registry.successor = reloaded
    with pytest.raises(spend_guard.SpendDenied, match="no longer sets a budget"):
        _send(guard)


@pytest.mark.django_db(transaction=True)
def test_rr_concurrent_reservations_across_revisions_share_one_lock():
    """Two revisions of one envelope (a superseded one still held by queued
    work, and the current one), $1 each, four $0.40 attempts at once:
    exactly two fit, because both take the envelope's lock and total."""
    if connection.vendor != "postgresql":
        pytest.skip("advisory locks need PostgreSQL")
    lee = User.objects.create(username="lee")
    one = POOLS.replace("budget_usd: 10", "budget_usd: 1").replace(
        "    per_launch_usd: 2\n", ""
    )
    first = _own(parse_providers(one), lee).grant
    tighter = one.replace("    tiers: {", "    max_output_tokens: 8000\n    tiers: {")
    providers = parse_providers(tighter)
    second = _own(providers, lee).grant
    assert second.envelope_id == first.envelope_id and second.pk != first.pk
    results = []
    barrier = threading.Barrier(4)

    def attempt(revision):
        from django.db import connections

        barrier.wait()
        try:
            _hold(revision, lee, "0.40", providers)
            results.append("ok")
        except spend_guard.SpendDenied:
            results.append("denied")
        finally:
            connections.close_all()

    threads = [
        threading.Thread(target=attempt, args=(r,))
        for r in (first, second, first, second)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["denied", "denied", "ok", "ok"]
    assert spend_guard.spent_and_reserved(first.pk) == (Decimal(0), Decimal("0.80"))


@pytest.mark.django_db
def test_rr_uncertain_holds_stay_counted_after_revocation():
    """A send that timed out keeps its hold through revocation, ownership
    moves and restoration: every later revision of the envelope counts it."""
    registry, bot = _config(POOLS)
    lee = User.objects.create(username="lee")
    guard = _guard(bot, lee)

    async def timeout():
        msg = "mock uncertain send"
        raise TimeoutError(msg)

    with pytest.raises(TimeoutError):
        async_to_sync(guard.call)(
            {"model": "gpt-6.1-sol", "messages": []}, "max_completion_tokens", timeout
        )
    row = InferenceRequest.objects.get(billing_mode="metered")
    assert row.cost_status == "unknown" and row.reserved_usd > 0
    registry.providers = parse_providers(NO_BUDGET)
    assert _own(bot.providers, lee).grant is None
    row.refresh_from_db()
    assert row.cost_status == "unknown"  # revocation never releases a hold
    registry.providers = parse_providers(POOLS)
    restored = _own(bot.providers, lee)
    assert restored.reserved_usd == row.reserved_usd


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


# -- Migrations: 0059 then 0060 from a main-0058 DB, the backfill, and back -----


def _executor():
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    return executor


def _to_leaves():
    executor = _executor()
    executor.migrate(executor.loader.graph.leaf_nodes())


POOL_TABLES = {
    "django_ergo_spendgrant",
    "django_ergo_spendenvelope",
    "django_ergo_modelpoolstate",
    "django_ergo_modelpoolpreference",
    "django_ergo_routedecision",
}
LEAF = ("django_ergo", "0061_spend_envelope_families")


@pytest.mark.django_db(transaction=True)
def test_rr_migrations_upgrade_from_main_0058_and_reverse():
    from django_ergo.conversation.models import AgentLaunch

    def present():
        return POOL_TABLES & set(connection.introspection.table_names())

    assert present() == POOL_TABLES
    try:
        _executor().migrate([("django_ergo", "0058_agent_launch_intent")])
        assert present() == set()
        assert AgentLaunch._meta.db_table in connection.introspection.table_names()
        executor = _executor()
        plan = [m.name for m, _ in executor.migration_plan([LEAF])]
        assert plan == [
            "0059_model_pools",
            "0060_spend_envelopes",
            "0061_spend_envelope_families",
        ]
        executor.migrate([LEAF])
        assert present() == POOL_TABLES
    finally:
        _to_leaves()


@pytest.mark.django_db(transaction=True)
def test_rr_0060_backfills_envelopes_conservatively_and_reverses():
    """Config grants of one pool and owner join one envelope (their spend
    and holds all count); an issued grant is its own; no ledger row changes.
    Reversing revokes superseded revisions and clears repeated config keys
    so 0059's unique constraint comes back."""
    before = ("django_ergo", "0059_model_pools")
    try:
        executor = _executor()
        executor.migrate([before])
        apps = executor.loader.project_state([before]).apps
        user = apps.get_model(*settings_user().split("."))
        grant_model = apps.get_model("django_ergo", "SpendGrant")
        request_model = apps.get_model("django_ergo", "InferenceRequest")
        lee = user.objects.create(username="lee")
        friend = user.objects.create(username="friend")
        now = timezone.now()
        common = {
            "pool": "paid",
            "owner_id": lee.pk,
            "routes": ["openai/gpt-6.1-sol"],
            "max_output_tokens": 100,
            "valid_from": now,
            "expires_at": now + dt.timedelta(days=7),
        }
        old = grant_model.objects.create(
            grantee_id=lee.pk,
            budget_usd=10,
            source="config",
            config_key="a",
            revoked_at=now,
            **common,
        )
        new = grant_model.objects.create(
            grantee_id=lee.pk,
            budget_usd=1000,
            source="config",
            config_key="b",
            **common,
        )
        issued = grant_model.objects.create(
            grantee_id=friend.pk, budget_usd=3, **common
        )
        for row, amount in ((old, 4), (new, 1), (issued, 2)):
            request_model.objects.create(
                pool=spend_guard.ledger_key(row.pk),
                provider="openai",
                model="gpt-6.1-sol",
                billing_mode="metered",
                reserved_usd=Decimal(amount),
                cost_status="unknown",
            )
        executor = _executor()
        executor.migrate([LEAF])
        from django_ergo.conversation.models import SpendGrant

        rows = {g.pk: g for g in SpendGrant.objects.select_related("envelope")}
        assert rows[old.pk].envelope_id == rows[new.pk].envelope_id
        assert rows[old.pk].envelope.key == f"config:paid:{lee.pk}"
        assert rows[issued.pk].envelope.key == f"issued:{issued.pk}"
        # 0061: the issued envelope is drawn from lee's config envelope.
        assert rows[issued.pk].envelope.parent_id == rows[old.pk].envelope_id
        assert rows[old.pk].envelope.accounts == ["provider:openai"]
        assert rows[old.pk].revoked_at is not None  # nothing un-revoked
        assert spend_guard.spent_and_reserved(new.pk) == (Decimal(0), Decimal(5))
        assert spend_guard.spent_and_reserved(issued.pk) == (Decimal(0), Decimal(2))
        # A later revision with a repeated fingerprint, superseding "b".
        SpendGrant.objects.filter(pk=new.pk).update(superseded_at=now)
        SpendGrant.objects.create(
            envelope_id=rows[new.pk].envelope_id,
            grantee_id=lee.pk,
            budget_usd=10,
            source="config",
            config_key="a",
            **common,
        )
        executor = _executor()
        executor.migrate([before])
        apps = executor.loader.project_state([before]).apps
        grant_model = apps.get_model("django_ergo", "SpendGrant")
        assert grant_model.objects.get(pk=new.pk).revoked_at is not None
        # 0059 totals per grant: the envelope's rows all moved to its current
        # revision, so rolling back refunds nothing.
        current = grant_model.objects.get(config_key="a", revoked_at__isnull=True)
        request_model = apps.get_model("django_ergo", "InferenceRequest")
        assert set(request_model.objects.values_list("pool", flat=True)) == {
            spend_guard.ledger_key(current.pk),
            spend_guard.ledger_key(issued.pk),
        }
        assert (
            request_model.objects.filter(
                pool=spend_guard.ledger_key(current.pk)
            ).count()
            == 2
        )
        keys = list(
            grant_model.objects.exclude(config_key="").values_list(
                "config_key", flat=True
            )
        )
        assert sorted(keys) == ["a", "b"]
    finally:
        _to_leaves()


def settings_user() -> str:
    from django.conf import settings

    return settings.AUTH_USER_MODEL
