"""Spend authority that can't widen or outlive revocation (#404, after N1).

1. Envelope identity survives config renames: a renamed, or removed and
   re-added, pool continues its envelope; a new pool on already-funded
   accounts gets no money from config sync until an admin links it.
2. Issued grants are drawn from the owner's config authority: every send is
   intersected with the owner's current terms, and the owner's budget caps
   the owner's and every grantee's spend together.
3. Config skew: a process holding an older providers.yaml re-reads the file
   at each guarded send (and at sync); an unreadable file fails closed for
   metered sends only.

Mock transports only: no network, no paid inference.
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
from django_ergo.bots.definition import BotDefinition
from django_ergo.bots.providers import load_providers
from django_ergo.bots.providers import parse_providers
from django_ergo.bots.registry import BotRegistry
from django_ergo.bots.runtime import Bot
from django_ergo.conversation import spend_guard
from django_ergo.conversation.models import InferenceRequest
from django_ergo.conversation.models import SpendEnvelope
from django_ergo.conversation.models import SpendGrant
from tests.pool_funding import fund
from tests.test_model_pools import POOLS
from tests.test_model_pools import chat
from tests.test_model_pools import environment  # noqa: F401 -- fake keys and CLIs
from tests.test_model_pools import grant
from tests.test_model_pools_rereview import NO_BUDGET
from tests.test_model_pools_rereview import _config
from tests.test_model_pools_rereview import _guard
from tests.test_model_pools_rereview import _send

User = get_user_model()

RENAMED = POOLS.replace("  - name: paid\n", "  - name: paid-api\n")
# The paid pool removed: subscriptions only.
WITHOUT_PAID = (
    POOLS[: POOLS.index("  - name: paid\n")] + POOLS[POOLS.index("private_pools:") :]
)
# A second owned pool on the OpenAI account the paid pool already funds.
EXTRA = POOLS.replace(
    "private_pools:",
    "  - name: extra\n"
    "    owner: lee\n"
    "    routes: [openai/gpt-6-luna]\n"
    "    budget_usd: 50\n"
    "    expires: 2099-12-31\n"
    "private_pools:",
)


def _view(providers, user, pool="paid"):
    return pools.snapshot(providers, user.pk).get(pool)


def _hold(grant_row, user, amount, providers, *, ref="openai/gpt-6.1-sol"):
    provider, _, model = ref.partition("/")
    return spend_guard.reserve(
        grant_id=grant_row.pk,
        provider=provider,
        model=model,
        amount=Decimal(amount),
        user_id=user.pk,
        fields={"bot_name": "devbox", "selector": ref},
        current=pools.SendAuthority(providers),
    )


def _admin():
    return User.objects.create(username="admin", is_superuser=True)


# -- 1. envelope identity survives renames -------------------------------------


@pytest.mark.django_db
def test_rename_continues_the_envelope_and_its_cumulative_spend():
    """paid -> paid-api with $6 held: the renamed pool's authority is a new
    revision of the SAME envelope, so only $4 of the $10 is left."""
    lee = User.objects.create(username="lee")
    fund(parse_providers(POOLS), lee)
    before = _view(parse_providers(POOLS), lee).grant
    _hold(before, lee, "6", parse_providers(POOLS))
    renamed = parse_providers(RENAMED)
    after = _view(renamed, lee, "paid-api")
    assert after.grant.envelope_id == before.envelope_id
    assert after.grant.pk != before.pk and after.grant.pool == "paid-api"
    assert after.reserved_usd == Decimal(6)
    envelope = SpendEnvelope.objects.get(pk=before.envelope_id)
    assert envelope.key == f"config:paid-api:{lee.pk}"
    assert "continued from pool 'paid'" in envelope.link_note
    assert not SpendEnvelope.objects.filter(key=f"config:paid:{lee.pk}").exists()
    with pytest.raises(spend_guard.SpendDenied, match="at its budget"):
        _hold(after.grant, lee, "4.01", renamed)
    _hold(after.grant, lee, "4", renamed)
    # A guard still holding the old name's revision follows its envelope:
    # the same cumulative total, so nothing is left for it either.
    with pytest.raises(spend_guard.SpendDenied, match="at its budget"):
        _hold(before, lee, "0.01", renamed)
    # Renaming back continues it again: still one envelope, nothing new.
    back = _view(parse_providers(POOLS), lee)
    assert back.grant.envelope_id == before.envelope_id
    assert back.reserved_usd == Decimal(10)
    assert SpendEnvelope.objects.filter(source="config").count() == 1


@pytest.mark.django_db
def test_remove_and_readd_under_a_new_name_keeps_the_cumulative_spend():
    lee = User.objects.create(username="lee")
    fund(parse_providers(POOLS), lee)
    first = _view(parse_providers(POOLS), lee).grant
    _hold(first, lee, "7", parse_providers(POOLS))
    assert _view(parse_providers(WITHOUT_PAID), lee) is None
    first.refresh_from_db()
    assert first.revoked_at is not None  # removed: config sync revoked it
    readded = parse_providers(RENAMED.replace("budget_usd: 10", "budget_usd: 10"))
    view = _view(readded, lee, "paid-api")
    assert view.grant.envelope_id == first.envelope_id
    assert (view.spent_usd, view.reserved_usd) == (Decimal(0), Decimal(7))
    with pytest.raises(spend_guard.SpendDenied, match="at its budget"):
        _hold(view.grant, lee, "3.01", readded)


@pytest.mark.django_db
def test_rename_keeps_a_persons_revocation():
    """lee revoked their config authority; renaming the pool (same money
    terms) doesn't mint it again."""
    lee = User.objects.create(username="lee")
    fund(parse_providers(POOLS), lee)
    providers = parse_providers(POOLS)
    own = _view(providers, lee).grant
    pools.revoke_grant(providers, own.pk, by_id=lee.pk)
    view = _view(parse_providers(RENAMED), lee, "paid-api")
    assert view.grant is None
    assert SpendEnvelope.objects.get(pk=own.envelope_id).pool == "paid-api"


@pytest.mark.django_db
def test_a_new_pool_on_funded_accounts_gets_no_fresh_money():
    """A second pool on the OpenAI account paid already funds: no envelope
    from config sync (not selectable, refused at send), until an admin links
    it: continuing a funded envelope (refused while another pool uses it)
    or funding it separately (explicit new money)."""
    lee = User.objects.create(username="lee")
    fund(parse_providers(POOLS), lee)
    providers = parse_providers(EXTRA)
    paid = _view(providers, lee)
    assert paid.grant is not None
    extra = _view(providers, lee, "extra")
    assert extra.grant is None
    code, reason = extra.grant_problem()
    assert code == pools.BUDGET_REQUIRED and "an admin must link" in reason
    assert not SpendEnvelope.objects.filter(key=f"config:extra:{lee.pk}").exists()
    _registry, bot = _config(EXTRA)
    session = chat(bot, lee)
    with pytest.raises(pools.PoolRefusal):
        pools.select_route(bot, session, ref="extra/openai/gpt-6-luna")
    # Only an admin links; and not to an envelope a current pool still uses.
    with pytest.raises(PermissionError):
        pools.link_config_envelope(
            providers, "extra", by_id=lee.pk, admin=False, fund=True
        )
    admin = _admin()
    with pytest.raises(ValueError, match="still uses"):
        pools.link_config_envelope(
            providers,
            "extra",
            by_id=admin.pk,
            admin=True,
            envelope_id=paid.grant.envelope_id,
        )
    envelope = pools.link_config_envelope(
        providers, "extra", by_id=admin.pk, admin=True, fund=True, note="separate $50"
    )
    assert envelope.linked_by_id == admin.pk and envelope.link_note == "separate $50"
    fund(providers, lee, pool="extra")
    extra = _view(providers, lee, "extra")
    assert extra.grant.envelope_id == envelope.pk != paid.grant.envelope_id
    assert extra.grant.budget_usd == 50


@pytest.mark.django_db
def test_a_renamed_provider_on_the_same_key_is_the_same_account():
    """Renaming the provider (same key env) and the pool together is still
    the same money: it continues the envelope."""
    lee = User.objects.create(username="lee")
    fund(parse_providers(POOLS), lee)
    first = _view(parse_providers(POOLS), lee).grant
    _hold(first, lee, "9", parse_providers(POOLS))
    text = (
        RENAMED.replace("  openai:\n    type: openai\n", "  oai:\n    type: openai\n")
        .replace("routes: [openai, anthropic]", "routes: [oai, anthropic]")
        .replace(
            "tiers: {medium: [openai/gpt-6.1-sol, ",
            "tiers: {medium: [oai/gpt-6.1-sol, ",
        )
    )
    view = _view(parse_providers(text), lee, "paid-api")
    assert view.grant.envelope_id == first.envelope_id
    assert view.reserved_usd == Decimal(9)


@pytest.mark.django_db
def test_merging_two_funded_pools_waits_for_an_admin():
    """Two envelopes (OpenAI, Anthropic) both overlap a merged pool: which
    ledger carries over is ambiguous, so config sync funds nothing; an admin
    continues one."""
    lee = User.objects.create(username="lee")
    fund(parse_providers(POOLS), lee)
    two = (
        POOLS.replace("routes: [openai, anthropic]", "routes: [openai]")
        .replace(
            "tiers: {medium: [openai/gpt-6.1-sol, anthropic/claude-opus-5-5]}",
            "tiers: {medium: [openai/gpt-6.1-sol]}",
        )
        .replace(
            "private_pools:",
            "  - name: claude-api\n"
            "    owner: lee\n"
            "    routes: [anthropic]\n"
            "    budget_usd: 5\n"
            "    expires: 2099-12-31\n"
            "private_pools:",
        )
    )
    providers = parse_providers(two)
    openai_env = _view(providers, lee).grant.envelope_id
    preadmin = User.objects.create(username="preadmin", is_superuser=True)
    pools.link_config_envelope(
        providers, "claude-api", by_id=preadmin.pk, admin=True, fund=True
    )
    fund(providers, lee, pool="claude-api")
    anthropic_env = _view(providers, lee, "claude-api").grant.envelope_id
    merged = parse_providers(RENAMED)  # paid-api on both accounts
    view = _view(merged, lee, "paid-api")
    assert view.grant is None and "an admin must link" in view.grant_problem()[1]
    admin = _admin()
    pools.link_config_envelope(
        merged, "paid-api", by_id=admin.pk, admin=True, envelope_id=openai_env
    )
    view = _view(merged, lee, "paid-api")
    assert view.grant.envelope_id == openai_env != anthropic_env


# -- 2. issued grants are drawn from the owner's current authority --------------


@pytest.mark.django_db
def test_issuing_needs_the_owners_own_authority():
    _registry, bot = _config(NO_BUDGET)
    User.objects.create(username="lee")
    friend = User.objects.create(username="friend")
    with pytest.raises(ValueError, match="no spend authority of their own"):
        grant(bot.providers, friend, budget="3")


@pytest.mark.django_db
def test_lowering_the_owners_budget_narrows_an_issued_grant_and_its_built_guard():
    """lee $10, friend's grant $10. lee holds $6: friend can reserve $4 at
    most (one cap for the family). lee lowers the budget to $7: a guard
    friend built before has $1 left; to $6: nothing."""
    registry, bot = _config(POOLS)
    lee = User.objects.create(username="lee")
    fund(parse_providers(POOLS), lee)
    friend = User.objects.create(username="friend")
    issued = grant(bot.providers, friend, budget="10")
    own = _view(bot.providers, lee).grant
    assert issued.envelope.parent_id == own.envelope_id
    _hold(own, lee, "6", bot.providers)
    with pytest.raises(spend_guard.SpendDenied, match="owner's budget is used"):
        _hold(issued, friend, "4.01", bot.providers)
    view = _view(bot.providers, friend)
    assert (view.cap_usd, view.family_used_usd) == (Decimal(10), Decimal(6))
    guard = _guard(bot, friend)
    assert guard.grant_id == issued.pk
    registry.providers = parse_providers(
        POOLS.replace("budget_usd: 10", "budget_usd: 7")
    )
    with pytest.raises(spend_guard.SpendDenied, match="owner's budget is used"):
        _hold(issued, friend, "1.01", bot.providers)
    _hold(issued, friend, "1", bot.providers)
    registry.providers = parse_providers(
        POOLS.replace("budget_usd: 10", "budget_usd: 6")
    )
    with pytest.raises(spend_guard.SpendDenied, match="owner's budget is used"):
        _send(guard)
    code, _ = _view(bot.providers, friend).grant_problem()
    assert code == pools.BUDGET_EXHAUSTED
    # The owner is capped by the family's spend too.
    with pytest.raises(spend_guard.SpendDenied, match="at its budget|owner's budget"):
        _hold(_view(bot.providers, lee).grant, lee, "0.01", bot.providers)


@pytest.mark.django_db
def test_removing_or_revoking_the_owners_budget_revokes_issued_grants_at_once():
    registry, bot = _config(POOLS)
    lee = User.objects.create(username="lee")
    fund(parse_providers(POOLS), lee)
    friend = User.objects.create(username="friend")
    issued = grant(bot.providers, friend, budget="3")
    guard = _guard(bot, friend)
    registry.providers = parse_providers(NO_BUDGET)
    with pytest.raises(spend_guard.SpendDenied, match="owner's spend authority"):
        _send(guard)
    assert _view(bot.providers, friend).grant_problem()[0] == pools.BUDGET_REQUIRED
    # Restored: the friend works again (same envelopes, nothing replenished).
    registry.providers = parse_providers(POOLS)
    _send(guard)
    # The owner revokes their own authority: the friend's guard stops too.
    own = _view(bot.providers, lee).grant
    pools.revoke_grant(bot.providers, own.pk, by_id=lee.pk)
    with pytest.raises(spend_guard.SpendDenied, match="owner's spend authority"):
        _send(guard)
    issued.refresh_from_db()
    assert issued.revoked_at is None  # suspended with the owner's, not destroyed


@pytest.mark.django_db
def test_the_owners_expiry_routes_and_per_launch_narrow_an_issued_grant():
    registry, bot = _config(POOLS)
    User.objects.create(username="lee")
    friend = User.objects.create(username="friend")
    issued = grant(bot.providers, friend, budget="5", days=30, per_launch_usd="2")
    current = pools.SendAuthority(bot)
    # Owner's expiry earlier than the grant's: the grant ends with it.
    soon = (timezone.now() + dt.timedelta(days=2)).isoformat()
    registry.providers = parse_providers(
        POOLS.replace("expires: 2099-12-31", f"expires: '{soon}'")
    )
    terms = spend_guard.authorize(
        grant_id=issued.pk, user_id=friend.pk, current=current
    )
    assert terms.expires_at <= timezone.now() + dt.timedelta(days=2)
    past = POOLS.replace("expires: 2099-12-31", "expires: 2020-01-01")
    registry.providers = parse_providers(past)
    with pytest.raises(spend_guard.SpendDenied, match="expired"):
        _hold(issued, friend, "0.01", bot.providers)
    # Owner narrows the pool to OpenAI: the friend's Anthropic route is gone.
    registry.providers = parse_providers(POOLS)
    _hold(issued, friend, "0.01", bot.providers, ref="anthropic/claude-opus-5-5")
    openai_only = POOLS.replace(
        "routes: [openai, anthropic]", "routes: [openai]"
    ).replace(
        "tiers: {medium: [openai/gpt-6.1-sol, anthropic/claude-opus-5-5]}",
        "tiers: {medium: [openai/gpt-6.1-sol]}",
    )
    registry.providers = parse_providers(openai_only)
    with pytest.raises(spend_guard.SpendDenied, match="doesn't cover"):
        _hold(issued, friend, "0.01", bot.providers, ref="anthropic/claude-opus-5-5")
    # The owner's per-launch amount is removed: no metered launches for anyone.
    registry.providers = parse_providers(POOLS.replace("    per_launch_usd: 2\n", ""))
    with pytest.raises(spend_guard.SpendDenied, match="per-launch"):
        spend_guard.reserve_launch(
            grant_id=issued.pk,
            pool="paid",
            provider="openai",
            model="gpt-6.1-sol",
            amount=Decimal(1),
            user_id=friend.pk,
            current=pools.SendAuthority(bot),
        )


@pytest.mark.django_db
def test_an_issued_grant_without_the_owners_envelope_authorizes_nothing():
    """A grant row with no parent envelope (an owner with no config
    authority) refuses every send."""
    _registry, bot = _config(POOLS)
    lee = User.objects.create(username="lee")
    friend = User.objects.create(username="friend")
    orphan = SpendGrant.objects.create(
        pool="paid",
        owner=lee,
        grantee=friend,
        routes=["openai/gpt-6.1-sol"],
        budget_usd=Decimal(5),
        max_output_tokens=100,
        valid_from=timezone.now(),
        expires_at=timezone.now() + dt.timedelta(days=1),
    )
    assert orphan.envelope.parent_id is None
    with pytest.raises(
        spend_guard.SpendDenied, match="no spend authority of their own"
    ):
        _hold(orphan, friend, "0.01", bot.providers)


@pytest.mark.django_db(transaction=True)
def test_one_cap_for_the_owner_and_every_grantee_under_concurrency():
    """lee $1; two grantees with $1 grants each; six $0.40 attempts at once
    (owner and grantees): exactly two fit under lee's $1."""
    if connection.vendor != "postgresql":
        pytest.skip("advisory locks need PostgreSQL")
    one = POOLS.replace("budget_usd: 10", "budget_usd: 1").replace(
        "    per_launch_usd: 2\n", ""
    )
    providers = parse_providers(one)
    lee = User.objects.create(username="lee")
    fund(parse_providers(POOLS), lee)
    ann = User.objects.create(username="ann")
    ben = User.objects.create(username="ben")
    own = _view(providers, lee).grant
    grants = {
        lee: own,
        ann: grant(providers, ann, budget="1"),
        ben: grant(providers, ben, budget="1"),
    }
    results = []
    barrier = threading.Barrier(6)

    def attempt(user):
        from django.db import connections

        barrier.wait()
        try:
            _hold(grants[user], user, "0.40", providers)
            results.append("ok")
        except spend_guard.SpendDenied:
            results.append("denied")
        finally:
            connections.close_all()

    threads = [threading.Thread(target=attempt, args=(u,)) for u in (lee, ann, ben) * 2]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["denied"] * 4 + ["ok"] * 2
    held = spend_guard.family_spent_and_reserved(own.envelope_id)
    assert held == (Decimal(0), Decimal("0.80"))


# -- 3. config skew between processes --------------------------------------------


def _process(folder):
    """One process's view: providers.yaml loaded once from ``folder`` (never
    reloaded), with a bot on it."""
    registry = BotRegistry()
    registry.providers = load_providers([folder])
    bot = Bot(BotDefinition.from_dict({"name": "devbox", "engine": {}}))
    registry.add(bot)
    return registry, bot


@pytest.mark.django_db
def test_a_process_with_a_stale_config_refuses_after_revocation(tmp_path):
    """Two processes load providers.yaml; the file then drops the budget.
    The second process never reloads, yet its already-built guard refuses
    and its selection refuses: each re-reads the file. Narrowing (a lower
    budget) applies the same way."""
    lee = User.objects.create(username="lee")
    fund(parse_providers(POOLS), lee)
    friend = User.objects.create(username="friend")
    path = tmp_path / "providers.yaml"
    path.write_text(POOLS)
    _first, web = _process(tmp_path)
    stale, worker = _process(tmp_path)
    loaded = stale.providers
    issued = grant(web.providers, friend, budget="5")
    guard = _guard(worker, lee)
    friends = _guard(worker, friend)
    _send(guard)
    path.write_text(POOLS.replace("budget_usd: 10", "budget_usd: 0.01"))
    with pytest.raises(spend_guard.SpendDenied, match="budget"):
        _send(friends)  # narrowed: the owner's budget is spent already
    path.write_text(NO_BUDGET)
    assert stale.providers is loaded  # no reload happened
    for built in (guard, friends):
        with pytest.raises(spend_guard.SpendDenied, match="no longer sets a budget"):
            _send(built)
    with pytest.raises(pools.PoolRefusal):
        pools.select_route(worker, chat(worker, lee), ref="openai/gpt-6.1-sol")
    issued.refresh_from_db()
    assert issued.revoked_at is None
    assert (
        SpendGrant.objects.filter(source="config", revoked_at__isnull=True).count() == 0
    )
    # The stale process syncing again writes nothing wider back.
    pools.sync_authority(loaded)
    assert (
        SpendGrant.objects.filter(source="config", revoked_at__isnull=True).count() == 0
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "broken",
    ["pools: [\n  - {name: paid", "pools:\n  - name: paid\n    routes: [nope]\n"],
    ids=["yaml", "invalid"],
)
def test_an_unreadable_config_fails_closed_for_metered_only(tmp_path, broken):
    lee = User.objects.create(username="lee")
    fund(parse_providers(POOLS), lee)
    path = tmp_path / "providers.yaml"
    path.write_text(POOLS)
    _registry, bot = _process(tmp_path)
    guard = _guard(bot, lee)
    own = _view(bot.providers, lee).grant
    path.write_text(broken)
    with pytest.raises(spend_guard.SpendDenied, match="doesn't load"):
        _send(guard)
    with pytest.raises(spend_guard.SpendDenied, match="doesn't load"):
        _hold(own, lee, "0.01", bot.providers)
    # Nothing was revoked or re-issued from a config that doesn't load.
    own.refresh_from_db()
    assert own.revoked_at is None and own.superseded_at is None
    # Subscription routes keep working normally.
    session = chat(bot, lee)
    pools.update_preference(
        lee.pk,
        bot.providers,
        version=pools.preference(lee.pk).version,
        active_pool="subs",
    )
    decision = pools.select_route(bot, session, ref="claude/claude-opus-5-5")
    assert not decision.metered and decision.guard(bot, session) is None
    # Fixed again: metered sends work again.
    path.write_text(POOLS)
    _send(guard)


@pytest.mark.django_db
def test_link_candidates_list_the_overlapping_envelopes_for_an_admin():
    lee = User.objects.create(username="lee")
    fund(parse_providers(POOLS), lee)
    providers = parse_providers(EXTRA)
    paid = _view(providers, lee).grant
    _hold(paid, lee, "2", providers)
    found = pools.link_candidates(providers, "extra")
    assert "an admin must link" in found["problem"]
    (row,) = found["envelopes"]
    assert row["id"] == str(paid.envelope_id) and row["pool"] == "paid"
    assert row["in_use"] and not row["continuable"]
    assert Decimal(row["reserved_usd"]) == 2


# -- review follow-ups ------------------------------------------------------------


def _send_priced(guard, *, prompt=1000, output=0):
    from asgiref.sync import async_to_sync

    async def send():
        usage = SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=output,
            total_tokens=prompt + output,
            prompt_tokens_details=None,
            completion_tokens_details=None,
        )
        return SimpleNamespace(id="mock", usage=usage)

    return async_to_sync(guard.call)(
        {"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "hi"}]},
        "max_completion_tokens",
        send,
    )


@pytest.mark.django_db
def test_a_stale_process_prices_with_the_current_providers_yaml(tmp_path):
    """The price is re-read at send too: raised 100x, the stale guard
    reserves and prices at the new rate; removed, it refuses."""
    lee = User.objects.create(username="lee")
    fund(parse_providers(POOLS), lee)
    path = tmp_path / "providers.yaml"
    path.write_text(POOLS)
    _registry, bot = _process(tmp_path)
    guard = _guard(bot, lee)
    path.write_text(
        POOLS.replace(
            "{input: 1, cached: 0.1, output: 10}",
            "{input: 100, cached: 10, output: 10}",
        )
    )
    _send_priced(guard, prompt=10_000)
    row = InferenceRequest.objects.latest("pk")
    assert row.cost_usd == Decimal(1)  # 10k tokens at $100 per million
    path.write_text(
        POOLS.replace(
            "      - {name: gpt-6.1-sol, price: {input: 1, cached: 0.1, output: 10}}\n",
            "      - gpt-6.1-sol\n",
        )
    )
    with pytest.raises(spend_guard.SpendDenied, match="no exact price"):
        _send_priced(guard)


@pytest.mark.django_db
def test_a_replaced_grant_isnt_revived_by_renaming_back():
    lee = User.objects.create(username="lee")
    fund(parse_providers(POOLS), lee)
    friend = User.objects.create(username="friend")
    old = grant(parse_providers(POOLS), friend, budget="9")
    renamed = parse_providers(RENAMED)
    owners_view = _view(renamed, lee, "paid-api")
    assert str(old.pk) in {g["id"] for g in pools.issued_grants(owners_view)}
    grant(renamed, friend, pool="paid-api", budget="1")
    old.refresh_from_db()
    assert old.revoked_at is not None  # replaced in the same family
    back = parse_providers(POOLS)
    assert _view(back, friend) is None or _view(back, friend).grant is None
    with pytest.raises(spend_guard.SpendDenied):
        _hold(old, friend, "0.01", back)


@pytest.mark.django_db
def test_the_owner_can_revoke_a_grant_issued_under_the_pools_old_name():
    lee = User.objects.create(username="lee")
    fund(parse_providers(POOLS), lee)
    friend = User.objects.create(username="friend")
    old = grant(parse_providers(POOLS), friend, budget="9")
    renamed = parse_providers(RENAMED)
    _view(renamed, lee, "paid-api")
    pools.revoke_grant(renamed, old.pk, by_id=lee.pk)
    old.refresh_from_db()
    assert old.revoked_at is not None


@pytest.mark.django_db
def test_a_detour_through_an_empty_account_set_funds_nothing():
    """Rename onto a metered provider with no models (no accounts), then
    back onto the funded routes: no second budget on the OpenAI key."""
    lee = User.objects.create(username="lee")
    fund(parse_providers(POOLS), lee)
    first = _view(parse_providers(POOLS), lee).grant
    _hold(first, lee, "10", parse_providers(POOLS))
    stub = (
        RENAMED.replace(
            "  xai-oauth:",
            "  stub: {type: openai, api_key_env: TEST_POOL_OPENAI_KEY_X, "
            "billing: metered, models: []}\n  xai-oauth:",
        )
        .replace("routes: [openai, anthropic]", "routes: [stub]")
        .replace(
            "    tiers: {medium: [openai/gpt-6.1-sol, anthropic/claude-opus-5-5]}\n", ""
        )
    )
    _view(parse_providers(stub), lee, "paid-api")
    assert not SpendEnvelope.objects.filter(key=f"config:paid-api:{lee.pk}").exists()
    view = _view(parse_providers(RENAMED), lee, "paid-api")
    assert view.grant.envelope_id == first.envelope_id
    with pytest.raises(spend_guard.SpendDenied, match="at its budget"):
        _hold(view.grant, lee, "0.01", parse_providers(RENAMED))


@pytest.mark.django_db
def test_growing_onto_another_envelopes_account_waits_for_an_admin():
    """extra (funded separately by an admin, OpenAI) later adds Anthropic,
    which paid's envelope funds: extra's authority stops until an admin
    approves the new account."""
    lee = User.objects.create(username="lee")
    fund(parse_providers(POOLS), lee)
    admin = _admin()
    providers = parse_providers(EXTRA)
    _view(providers, lee)
    pools.link_config_envelope(
        providers, "extra", by_id=admin.pk, admin=True, fund=True
    )
    fund(providers, lee, pool="extra")
    extra = _view(providers, lee, "extra").grant
    _hold(extra, lee, "1", providers, ref="openai/gpt-6-luna")
    grown = parse_providers(
        EXTRA.replace(
            "routes: [openai/gpt-6-luna]", "routes: [openai/gpt-6-luna, anthropic]"
        )
    )
    view = _view(grown, lee, "extra")
    assert view.grant is None
    assert "an admin must approve" in view.grant_problem()[1]
    with pytest.raises(spend_guard.SpendDenied, match="revoked"):
        _hold(extra, lee, "0.01", grown, ref="openai/gpt-6-luna")
    pools.link_config_envelope(grown, "extra", by_id=admin.pk, admin=True, fund=True)
    view = _view(grown, lee, "extra")
    assert view.grant.envelope_id == extra.envelope_id
    assert view.reserved_usd == Decimal(1)


@pytest.mark.django_db
def test_a_provider_without_a_key_env_is_identified_by_the_sdk_default():
    lee = User.objects.create(username="lee")
    fund(parse_providers(POOLS), lee)
    base = POOLS.replace("    api_key_env: TEST_POOL_OPENAI_KEY\n", "")
    fund(parse_providers(base), lee)
    first = _view(parse_providers(base), lee).grant
    _hold(first, lee, "10", parse_providers(base))
    moved = (
        base.replace("  - name: paid\n", "  - name: paid-api\n")
        .replace("  openai:\n    type: openai\n", "  oai:\n    type: openai\n")
        .replace("routes: [openai, anthropic]", "routes: [oai, anthropic]")
        .replace(
            "tiers: {medium: [openai/gpt-6.1-sol, ",
            "tiers: {medium: [oai/gpt-6.1-sol, ",
        )
    )
    view = _view(parse_providers(moved), lee, "paid-api")
    assert view.grant.envelope_id == first.envelope_id


@pytest.mark.django_db
def test_runtime_switches_follow_a_rename():
    """A pool switched off stays off under its new name; a provider switched
    off stays off for spend under a new provider name on the same key."""
    from tests.test_model_pools import set_state

    lee = User.objects.create(username="lee")
    fund(parse_providers(POOLS), lee)
    providers = parse_providers(POOLS)
    own = _view(providers, lee).grant
    set_state("paid", providers, enabled=False)
    view = _view(parse_providers(RENAMED), lee, "paid-api")
    assert not view.enabled
    with pytest.raises(spend_guard.SpendDenied, match="switched off"):
        _hold(view.grant, lee, "0.01", parse_providers(RENAMED))
    set_state("paid-api", parse_providers(RENAMED), enabled=True)
    set_state("paid", providers, enabled=True)
    pools.set_provider_enabled("openai", providers, False, version=0)
    renamed_provider = (
        POOLS.replace("  openai:\n    type: openai\n", "  oai:\n    type: openai\n")
        .replace("routes: [openai, anthropic]", "routes: [oai, anthropic]")
        .replace(
            "tiers: {medium: [openai/gpt-6.1-sol, ",
            "tiers: {medium: [oai/gpt-6.1-sol, ",
        )
    )
    moved = parse_providers(renamed_provider)
    current = _view(moved, lee).grant
    assert current.envelope_id == own.envelope_id
    with pytest.raises(spend_guard.SpendDenied, match="doesn't cover"):
        _hold(current, lee, "0.01", moved, ref="oai/gpt-6.1-sol")


@pytest.mark.django_db
def test_an_admins_link_or_approval_never_lifts_a_persons_revocation():
    """lee revokes paid (a kill switch); paid merges with claude-api into
    paid-api, and an admin continues paid's envelope and approves the
    accounts: still revoked. Only restore=True lifts it."""
    lee = User.objects.create(username="lee")
    fund(parse_providers(POOLS), lee)
    two = (
        POOLS.replace("routes: [openai, anthropic]", "routes: [openai]")
        .replace(
            "tiers: {medium: [openai/gpt-6.1-sol, anthropic/claude-opus-5-5]}",
            "tiers: {medium: [openai/gpt-6.1-sol]}",
        )
        .replace(
            "private_pools:",
            "  - name: claude-api\n"
            "    owner: lee\n"
            "    routes: [anthropic]\n"
            "    budget_usd: 5\n"
            "    expires: 2099-12-31\n"
            "private_pools:",
        )
    )
    providers = parse_providers(two)
    paid = _view(providers, lee).grant
    _view(providers, lee, "claude-api")
    pools.revoke_grant(providers, paid.pk, by_id=lee.pk, reason="kill switch")
    merged = parse_providers(RENAMED)
    admin = _admin()
    pools.link_config_envelope(
        merged, "paid-api", by_id=admin.pk, admin=True, envelope_id=paid.envelope_id
    )
    assert _view(merged, lee, "paid-api").grant is None
    pools.link_config_envelope(
        merged, "paid-api", by_id=admin.pk, admin=True, fund=True
    )
    assert _view(merged, lee, "paid-api").grant is None
    pools.link_config_envelope(
        merged, "paid-api", by_id=admin.pk, admin=True, restore=True
    )
    restored = _view(merged, lee, "paid-api").grant
    assert restored.envelope_id == paid.envelope_id
