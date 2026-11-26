"""Synthetic UTC clocks, PostgreSQL locks, and mocked SDK attempts only."""

import datetime as dt
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.utils import timezone

from django_ergo.bots import pools
from django_ergo.bots.providers import parse_providers
from django_ergo.conversation import monthly_budgets as monthly
from django_ergo.conversation import spend_guard
from django_ergo.conversation.models import InferenceRequest
from django_ergo.conversation.models import ProviderAccountMapping
from django_ergo.conversation.models import ProviderBudgetFamily
from django_ergo.conversation.models import ProviderMonthlyConsent
from tests.pool_authority import authority
from tests.test_model_pools import POOLS
from tests.test_model_pools import environment  # noqa: F401 -- synthetic keys only

pytestmark = pytest.mark.django_db(transaction=True)
User = get_user_model()
END = dt.datetime(2099, 1, 1, tzinfo=dt.UTC)
START = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


@pytest.fixture
def clock(monkeypatch, settings):
    settings.USE_TZ = True
    value = [dt.datetime(2026, 11, 30, 23, 59, 59, tzinfo=dt.UTC)]
    monkeypatch.setattr(timezone, "now", lambda: value[0])
    return value


def consent(user, family="openai", amount="100", enabled=True, version=0):
    return monthly.set_consent(
        actor_id=user.pk,
        family_id=family,
        version=version,
        monthly_usd=amount,
        enabled=enabled,
        valid_from=START,
        expires_at=END,
        note="Explicit synthetic recurring consent",
    )


def setup_budget(clock, *, text=None, recurring=True, amount="100"):
    owner = User.objects.create(username="lee")
    admin = User.objects.create(username="admin", is_superuser=True)
    providers = parse_providers(
        text or POOLS.replace("budget_usd: 10", "budget_usd: 100")
    )
    config = providers.pools.get("paid")
    for ref in pools.pool_refs(providers, config, metered=True):
        head, _, model = ref.partition("/")
        monthly.assert_mapping(
            actor_id=admin.pk,
            owner_id=owner.pk,
            identity=pools.transport_identity(providers.providers[head], model),
            account_label=head,
            assertion="Admin asserts this vendor and account",
        )
    for family in ProviderBudgetFamily.objects.all():
        consent(owner, family.pk)
    pools.approve_funding(
        providers,
        "paid",
        by_id=owner.pk,
        budget_usd=amount,
        expires_at=END,
        valid_from=START,
        accounts=pools.funding_accounts(providers, config),
        budget_period=monthly.MONTHLY if recurring else monthly.CUMULATIVE,
        version=0,
        note="Synthetic pool approval",
    )
    grant = pools.snapshot(providers, owner.pk).get("paid").grant
    return owner, admin, providers, grant


def reserve(owner, providers, grant, amount, ref="openai/gpt-6.1-sol"):
    provider, _, model = ref.partition("/")
    return spend_guard.reserve(
        grant_id=grant.pk,
        provider=provider,
        model=model,
        amount=Decimal(amount),
        user_id=owner.pk,
        fields={},
        current=authority(providers, ref),
    )


def settle(pk, amount):
    spend_guard._finish(
        pk, cost_status="priced", cost_usd=Decimal(amount), outcome="ok"
    )


def test_utc_boundary_rolls_priced_usage_but_not_unknown_exposure(clock):
    owner, _, providers, grant = setup_budget(clock)
    pk = reserve(owner, providers, grant, "80")
    unknown = reserve(owner, providers, grant, "20")
    settle(pk, "80")
    with pytest.raises(spend_guard.SpendDenied, match="monthly budget"):
        reserve(owner, providers, grant, "0.0001")
    clock[0] += dt.timedelta(seconds=1)
    assert monthly.inspect_family("openai")["reserved_usd"] == "20.0000000000"
    reserve(owner, providers, grant, "80")
    with pytest.raises(spend_guard.SpendDenied):
        reserve(owner, providers, grant, "0.0001")
    row = InferenceRequest.objects.get(pk=unknown)
    assert row.budget_month == dt.date(2026, 11, 1)
    settle(unknown, "19")  # original November, never charges December
    reserve(owner, providers, grant, "20")
    assert monthly.inspect_family("openai")["spent_usd"] == "0"


def test_late_settlement_larger_than_hold_is_kept_in_original_month(clock):
    owner, _, providers, grant = setup_budget(clock)
    pk = reserve(owner, providers, grant, "80")
    clock[0] += dt.timedelta(seconds=1)
    settle(pk, "110")
    reserve(owner, providers, grant, "100")
    row = InferenceRequest.objects.get(pk=pk)
    assert row.cost_usd == 110 and row.budget_month == dt.date(2026, 11, 1)


def test_six_way_concurrency_uses_one_provider_bucket_across_envelopes(clock):
    owner, _, providers, grant = setup_budget(clock)
    friends = [User.objects.create(username=f"friend{i}") for i in range(5)]
    grants = [grant] + [
        pools.issue_grant(
            providers,
            "paid",
            issuer_id=owner.pk,
            grantee_id=f.pk,
            budget_usd="100",
            expires_at=END,
        )
        for f in friends
    ]
    barrier = threading.Barrier(6)

    def attempt(pair):
        user, row = pair
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            reserve(user, providers, row, "30")
            return "reserved"
        except spend_guard.SpendDenied:
            return "denied"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(
            executor.map(attempt, zip([owner, *friends], grants, strict=True))
        )
    assert results.count("reserved") == 3
    assert results.count("denied") == 3
    assert monthly.inspect_family("openai")["reserved_usd"] == "90.0000000000"


def test_multiple_keys_accounts_and_aliases_share_vendor_cap(clock):
    text = (
        POOLS.replace("budget_usd: 10", "budget_usd: 100")
        .replace(
            "  anthropic:\n",
            "  second:\n    type: openai\n    api_key_env: SECOND_SYNTHETIC_KEY\n    models: [{name: gpt-6.1-sol, price: {input: 1, output: 10}}]\n  anthropic:\n",
        )
        .replace("routes: [openai, anthropic]", "routes: [openai, second, anthropic]")
    )
    owner, _, providers, grant = setup_budget(clock, text=text)
    reserve(owner, providers, grant, "60")
    with pytest.raises(spend_guard.SpendDenied, match="monthly budget"):
        reserve(owner, providers, grant, "41", "second/gpt-6.1-sol")
    reserve(owner, providers, grant, "40", "second/gpt-6.1-sol")
    assert ProviderAccountMapping.objects.filter(family_id="openai").count() == 2


def test_provider_independence(clock):
    owner, _, providers, grant = setup_budget(
        clock, amount="300", text=POOLS.replace("budget_usd: 10", "budget_usd: 300")
    )
    reserve(owner, providers, grant, "100")
    reserve(owner, providers, grant, "100", "anthropic/claude-opus-5-5")
    assert monthly.inspect_family("anthropic")["reserved_usd"] == "100.0000000000"
    consent(owner, enabled=False, version=1)
    # OpenAI's switch does not pause Anthropic (only its own cap prevents this).
    with pytest.raises(spend_guard.SpendDenied, match="monthly budget"):
        reserve(owner, providers, grant, "1", "anthropic/claude-opus-5-5")


def test_existing_cumulative_grant_remains_additional_lifetime_cap(clock):
    owner, _, providers, grant = setup_budget(clock, recurring=False)
    settle(reserve(owner, providers, grant, "100"), "100")
    clock[0] += dt.timedelta(seconds=1)
    with pytest.raises(spend_guard.SpendDenied, match="grant is at its budget"):
        reserve(owner, providers, grant, "1")


def test_child_grant_remains_cumulative_when_owner_recurs(clock):
    owner, _, providers, _ = setup_budget(clock)
    friend = User.objects.create(username="friend")
    child = pools.issue_grant(
        providers,
        "paid",
        issuer_id=owner.pk,
        grantee_id=friend.pk,
        budget_usd="10",
        expires_at=END,
    )
    settle(reserve(friend, providers, child, "10"), "10")
    clock[0] += dt.timedelta(seconds=1)
    with pytest.raises(spend_guard.SpendDenied, match="grant is at its budget"):
        reserve(friend, providers, child, "1")


@pytest.mark.parametrize("change", ["lower", "higher", "reapply"])
def test_config_ceiling_cannot_expand_consent_or_reset_use(clock, change):
    owner, _, providers, grant = setup_budget(clock)
    reserve(owner, providers, grant, "60")
    text = POOLS.replace(
        "budget_usd: 10", "budget_usd: " + ("70" if change == "lower" else "1000")
    )
    altered = parse_providers(text)
    if change == "reapply":
        approval = grant.envelope.funding.last()
        pools.approve_funding(
            altered,
            "paid",
            by_id=owner.pk,
            budget_usd="100",
            expires_at=END,
            valid_from=START,
            accounts=pools.funding_accounts(altered, altered.pools.get("paid")),
            budget_period=monthly.MONTHLY,
            version=approval.pk,
        )
    current = pools.snapshot(altered, owner.pk).get("paid").grant
    assert current.envelope_id == grant.envelope_id
    with pytest.raises(spend_guard.SpendDenied):
        reserve(owner, altered, current, "11" if change == "lower" else "41")


def test_monthly_mapping_and_consent_are_immutable_and_versioned(clock):
    owner, admin, _, _ = setup_budget(clock)
    mapping = ProviderAccountMapping.objects.first()
    mapping.account_label = "renamed"
    with pytest.raises(TypeError, match="immutable"):
        mapping.save()
    with pytest.raises(TypeError, match="immutable"):
        ProviderAccountMapping.objects.filter(pk=mapping.pk).update(account_label="new")
    with pytest.raises(TypeError, match="immutable"):
        ProviderMonthlyConsent.objects.all().delete()
    with pytest.raises(pools.VersionConflict):
        consent(owner, version=0)
    consent(owner, enabled=False, version=1)
    with pytest.raises(ValueError, match="at most"):
        consent(admin, amount="101", version=2)


@pytest.mark.parametrize(
    "control", ["consent", "provider", "pool", "pause", "revocation", "privacy"]
)
def test_current_controls_narrow_recurring_authority(clock, control):
    owner, admin, providers, grant = setup_budget(clock)
    reserve(owner, providers, grant, "1")
    if control == "consent":
        consent(owner, enabled=False, version=1)
    elif control == "provider":
        pools.set_provider_enabled("openai", providers, False, version=0)
    elif control == "pool":
        pools.update_pool_state("paid", providers, version=0, enabled=False)
    elif control == "pause":
        from django_ergo.conversation.models import PaidDispatchState

        state = PaidDispatchState.objects.get(pk=1)
        pools.set_paid_pause(
            by_id=admin.pk, paused=True, version=state.updated_at.isoformat()
        )
    elif control == "revocation":
        pools.revoke_grant(providers, grant.pk, by_id=owner.pk)
    else:
        current = authority(providers)
        # Captured personal privacy refusing the route is checked on every attempt.
        from django_ergo.bots.pool_config import PrivacyPolicy

        current.privacy = pools.Privacy(
            (PrivacyPolicy(name="personal", allow=frozenset()),), frozenset()
        )
        with pytest.raises(spend_guard.SpendDenied, match="privacy"):
            spend_guard.reserve(
                grant_id=grant.pk,
                provider="openai",
                model="gpt-6.1-sol",
                amount=Decimal(1),
                user_id=owner.pk,
                fields={},
                current=current,
            )
        return
    with pytest.raises(spend_guard.SpendDenied):
        reserve(owner, providers, grant, "1")


def test_ambiguous_history_blocks_activation_then_explicit_mapping_keeps_exposure(
    clock,
):
    owner = User.objects.create(username="lee")
    admin = User.objects.create(username="admin", is_superuser=True)
    old = InferenceRequest.objects.create(
        provider="old-alias", model="x", billing_mode="metered", reserved_usd=70
    )
    providers = parse_providers(POOLS)
    identity = pools.transport_identity(providers.providers["openai"], "gpt-6.1-sol")
    monthly.assert_mapping(
        actor_id=admin.pk,
        owner_id=owner.pk,
        identity=identity,
        account_label="Account",
        assertion="Explicit account assertion",
    )
    with pytest.raises(ValueError, match="Ambiguous"):
        consent(owner)
    monthly.assert_mapping(
        actor_id=admin.pk,
        owner_id=owner.pk,
        identity=identity,
        account_label="Account",
        assertion="Explicit historical mapping",
        attempt_ids=[old.pk],
    )
    consent(owner)
    assert monthly.inspect_family("openai")["reserved_usd"] == "70.0000000000"
    clock[0] += dt.timedelta(days=35)
    assert monthly.inspect_family("openai")["reserved_usd"] == "70.0000000000"


def test_unbounded_historical_exposure_blocks_consent_even_after_mapping(clock):
    owner = User.objects.create(username="lee")
    admin = User.objects.create(username="admin", is_superuser=True)
    old = InferenceRequest.objects.create(
        pool=f"grant:{uuid.uuid4()}",
        provider="old",
        model="x",
        billing_mode="api",
        reserved_usd=None,
    )
    providers = parse_providers(POOLS)
    monthly.assert_mapping(
        actor_id=admin.pk,
        owner_id=owner.pk,
        identity=pools.transport_identity(providers.providers["openai"], "gpt-6.1-sol"),
        account_label="Account",
        assertion="Historical attestation",
        attempt_ids=[old.pk],
    )
    with pytest.raises(ValueError, match="no positive bound"):
        consent(owner)
    settle(old.pk, "8")
    consent(owner)
    assert monthly.inspect_family("openai")["spent_usd"] == "8.0000000000"


def test_exact_adapter_settles_monthly_reservation(clock):
    owner, _, providers, grant = setup_budget(clock)
    guard = spend_guard.MeteredGuard(
        pool="paid",
        provider="openai",
        model="gpt-6.1-sol",
        grant_id=grant.pk,
        current=authority(providers),
        user_id=owner.pk,
        price=providers.providers["openai"].models["gpt-6.1-sol"].price,
    )

    async def send():
        return SimpleNamespace(
            id="mock", usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10)
        )

    async_to_sync(guard.call)(
        {"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "hi"}]},
        spend_guard.OpenAIChatAdapter(),
        send,
    )
    row = InferenceRequest.objects.get()
    assert row.provider_family_id == "openai"
    assert row.cost_usd == Decimal("0.0002")
    assert row.cost_status == "priced"
    assert row.budget_month == dt.date(2026, 11, 1)


def test_same_provider_after_rename_and_key_alias_cannot_reset_allowance(clock):
    owner, admin, providers, grant = setup_budget(clock)
    reserve(owner, providers, grant, "90")
    renamed = parse_providers(
        POOLS.replace("budget_usd: 10", "budget_usd: 100")
        .replace("openai:", "renamed:")
        .replace("[openai, anthropic]", "[renamed, anthropic]")
        .replace("openai/gpt", "renamed/gpt")
    )
    current = pools.snapshot(renamed, owner.pk).get("paid").grant
    assert current.envelope_id == grant.envelope_id
    with pytest.raises(spend_guard.SpendDenied, match="monthly budget"):
        reserve(owner, renamed, current, "11", "renamed/gpt-6.1-sol")
    alias = parse_providers(
        POOLS.replace("budget_usd: 10", "budget_usd: 100").replace(
            "TEST_POOL_OPENAI_KEY", "NEW_SYNTHETIC_ALIAS"
        )
    )
    identity = pools.transport_identity(alias.providers["openai"], "gpt-6.1-sol")
    monthly.assert_mapping(
        actor_id=admin.pk,
        owner_id=owner.pk,
        identity=identity,
        account_label="Same billing account, new alias",
        assertion="Admin explicitly links new credential alias to same vendor",
    )
    approval = grant.envelope.funding.last()
    pools.approve_funding(
        alias,
        "paid",
        by_id=owner.pk,
        budget_usd="100",
        expires_at=END,
        valid_from=START,
        accounts=pools.funding_accounts(alias, alias.pools.get("paid")),
        budget_period=monthly.MONTHLY,
        version=approval.pk,
    )
    current = pools.snapshot(alias, owner.pk).get("paid").grant
    with pytest.raises(spend_guard.SpendDenied, match="monthly budget"):
        reserve(owner, alias, current, "11")


def test_paid_coding_remains_refused_under_recurring_funding(clock):
    owner, _, providers, grant = setup_budget(clock)
    with pytest.raises(spend_guard.SpendDenied, match="paid coding"):
        spend_guard.reserve_launch(
            grant_id=grant.pk,
            pool="paid",
            provider="openai",
            model="gpt-6.1-sol",
            amount=Decimal(1),
            user_id=owner.pk,
            current=authority(providers),
        )
    assert not InferenceRequest.objects.exists()


def test_calendar_boundary_and_utc_offsets(clock):
    clock[0] = dt.datetime(
        2027, 1, 31, 16, 0, tzinfo=dt.timezone(dt.timedelta(hours=-8))
    )
    assert monthly.month_of() == dt.date(2027, 2, 1)
    clock[0] -= dt.timedelta(microseconds=1)
    assert monthly.month_of() == dt.date(2027, 1, 1)
    owner, _, providers, grant = setup_budget(clock)
    settle(reserve(owner, providers, grant, "100"), "100")
    clock[0] += dt.timedelta(microseconds=1)
    reserve(owner, providers, grant, "100")


def test_configuration_removal_and_restore_preserves_provider_history(clock):
    owner, _, providers, grant = setup_budget(clock)
    reserve(owner, providers, grant, "90")
    without = parse_providers(POOLS[: POOLS.index("\npools:")])
    pools.sync_authority(without)
    pools.sync_authority(providers)
    current = pools.snapshot(providers, owner.pk).get("paid").grant
    assert current.envelope_id == grant.envelope_id
    with pytest.raises(spend_guard.SpendDenied, match="monthly budget"):
        reserve(owner, providers, current, "11")


def test_mapping_requires_admin_and_family_owner_cannot_be_changed(clock):
    owner, admin, providers, _ = setup_budget(clock)
    identity = pools.transport_identity(providers.providers["openai"], "gpt-6.1-sol")
    with pytest.raises(PermissionError):
        monthly.assert_mapping(
            actor_id=owner.pk,
            owner_id=owner.pk,
            identity=identity,
            account_label="x",
            assertion="Not an admin",
        )
    other = User.objects.create(username="other-owner")
    with pytest.raises(ValueError, match="owner is immutable"):
        monthly.assert_mapping(
            actor_id=admin.pk,
            owner_id=other.pk,
            identity=identity,
            account_label="x",
            assertion="Attempt owner replacement",
        )


def test_owner_envelope_serializes_different_provider_locks(clock):
    owner, _, providers, grant = setup_budget(clock)
    barrier = threading.Barrier(6)

    def attempt(index):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            ref = "openai/gpt-6.1-sol" if index % 2 else "anthropic/claude-opus-5-5"
            reserve(owner, providers, grant, "30", ref)
            return "reserved"
        except spend_guard.SpendDenied:
            return "denied"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(attempt, range(6)))
    assert results.count("reserved") == 3
    assert spend_guard.family_spent_and_reserved(grant.envelope_id)[1] == 90


def test_gemini_uses_same_exact_adapter_with_independent_monthly_bucket(clock):
    from django_ergo.conversation.engines.google_spend import GoogleAdapter
    from tests.test_conversation_google_spend import POOLS as GOOGLE_POOLS
    from tests.test_conversation_google_spend import body
    from tests.test_conversation_google_spend import usage_meta

    owner, _, providers, grant = setup_budget(
        clock, text=GOOGLE_POOLS.replace("budget_usd: 10", "budget_usd: 100")
    )
    guard = spend_guard.MeteredGuard(
        pool="paid",
        provider="gemini",
        model="gemini-2.5-pro",
        grant_id=grant.pk,
        current=authority(providers, "gemini/gemini-2.5-pro"),
        user_id=owner.pk,
        price=providers.providers["gemini"].models["gemini-2.5-pro"].price,
    )

    async def send():
        return {"usageMetadata": usage_meta()}

    async_to_sync(guard.call)(body(), GoogleAdapter(), send)
    row = InferenceRequest.objects.get()
    assert row.provider_family_id == "google"
    assert row.cost_status == "priced" and row.cost_usd > 0
    assert ProviderBudgetFamily.objects.count() == 1
    assert monthly.inspect_family("google")["reserved_usd"] == "0"


def test_explicit_monthly_conversion_renews_new_authority_but_preserves_queued_cap(
    clock,
):
    owner, _, providers, original = setup_budget(clock, recurring=False)
    settle(reserve(owner, providers, original, "100"), "100")
    previous = original.envelope.funding.last()
    pools.approve_funding(
        providers,
        "paid",
        by_id=owner.pk,
        budget_usd="100",
        expires_at=END,
        valid_from=START,
        accounts=pools.funding_accounts(providers, providers.pools.get("paid")),
        budget_period=monthly.MONTHLY,
        version=previous.pk,
        convert_period=True,
    )
    clock[0] += dt.timedelta(seconds=1)
    view = pools.snapshot(providers, owner.pk).get("paid")
    assert view.grant.budget_period == monthly.MONTHLY
    assert view.grant_problem() is None
    assert view.family_used_usd == 0
    reserve(owner, providers, view.grant, "1")
    with pytest.raises(spend_guard.SpendDenied, match="grant is at its budget"):
        reserve(owner, providers, original, "1")


def test_midnight_during_reservation_uses_one_month_for_every_cap(clock, monkeypatch):
    owner, _, providers, grant = setup_budget(clock, text=POOLS)
    settle(reserve(owner, providers, grant, "10"), "10")
    original_context = monthly.reservation_context

    def cross_boundary(*args, **kwargs):
        result = original_context(*args, **kwargs)
        clock[0] += dt.timedelta(seconds=1)
        return result

    monkeypatch.setattr(monthly, "reservation_context", cross_boundary)
    with pytest.raises(spend_guard.SpendDenied, match="grant is at its budget"):
        reserve(owner, providers, grant, "1")
    assert InferenceRequest.objects.count() == 1
