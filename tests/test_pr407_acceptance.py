"""Acceptance tests for the PR #407 review findings (P1-1, P2-1, P2-2).

The reviewer's ``test_repro_*`` tests are inverted here: they assert the fixed
behavior. ``test_control_*`` are the money invariants and must keep passing. Synthetic users, synthetic env names, mocked
transports and a synthetic UTC clock only; no provider is ever contacted.
"""

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

from django_ergo.bots import pools
from django_ergo.bots.providers import parse_providers
from django_ergo.conversation import monthly_budgets as monthly
from django_ergo.conversation import spend_guard
from django_ergo.conversation.models import InferenceRequest
from django_ergo.conversation.models import ProviderAccountMapping
from django_ergo.conversation.models import ProviderBudgetFamily
from django_ergo.conversation.models import SpendFunding
from tests.pool_authority import authority
from tests.test_model_pools import POOLS
from tests.test_model_pools import environment  # noqa: F401 -- synthetic keys only
from tests.test_monthly_provider_budgets import END
from tests.test_monthly_provider_budgets import START
from tests.test_monthly_provider_budgets import clock  # noqa: F401
from tests.test_monthly_provider_budgets import consent
from tests.test_monthly_provider_budgets import reserve
from tests.test_monthly_provider_budgets import settle
from tests.test_monthly_provider_budgets import setup_budget

pytestmark = pytest.mark.django_db(transaction=True)
User = get_user_model()


def private_fireworks_attempt():
    """Exactly what the live personal private pool writes for every attempt
    (conversation.inference_ledger._open -> InferenceRequest.objects.create)."""
    from django_ergo.conversation import inference_ledger

    pool = SimpleNamespace(name="personal", provider="fireworks", daily_usd_cap=None)
    grant = SimpleNamespace(
        pool=pool,
        model_id="accounts/fireworks/models/kimi-k3",
        session_id=None,
        user_id=None,
        bot="personal",
        selector="private:personal/heavy",
        purpose="chat",
    )
    entry = inference_ledger._open(grant, {"max_tokens": 100}, 1, None)
    return InferenceRequest.objects.get(pk=entry.pk)


# -- P1-1: the history gate is per vendor ---------------------------------------


def test_private_fireworks_attempt_never_blocks_recurring_budgets(clock):  # noqa: F811
    owner, admin, providers, grant = setup_budget(clock)
    reserve(owner, providers, grant, "1")
    row = private_fireworks_attempt()
    assert row.billing_mode == "api" and row.provider_family_id is None
    # Both enrolled vendors keep working, repeatedly, with no false assertion.
    reserve(owner, providers, grant, "1")
    private_fireworks_attempt()
    reserve(owner, providers, grant, "1", "anthropic/claude-opus-5-5")
    # ...and recurring consent can still be (re)enabled and is reported clean.
    consent(owner, version=1, enabled=False)
    consent(owner, version=2)
    assert monthly.inspect_family("openai")["history_problem"] == ""
    # The private row is not shared-vendor spend: nothing was filed under a vendor.
    assert monthly.inspect_family("openai")["spent_usd"] == "0"


def test_private_row_cannot_be_filed_under_a_vendor(clock):  # noqa: F811
    owner, admin, providers, grant = setup_budget(clock)
    row = private_fireworks_attempt()
    identity = pools.transport_identity(providers.providers["openai"], "gpt-6.1-sol")
    with pytest.raises(ValueError, match="paid provider API"):
        monthly.assert_mapping(
            actor_id=admin.pk,
            owner_id=owner.pk,
            identity=identity,
            account_label="openai",
            assertion="Forced: Fireworks attempt filed as OpenAI",
            attempt_ids=[row.pk],
        )


def test_unenrolled_vendor_attempt_blocks_only_its_own_vendor(clock):  # noqa: F811
    owner = User.objects.create(username="lee")
    admin = User.objects.create(username="admin", is_superuser=True)
    providers = parse_providers(POOLS.replace("budget_usd: 10", "budget_usd: 100"))
    monthly.assert_mapping(
        actor_id=admin.pk,
        owner_id=owner.pk,
        identity=pools.transport_identity(providers.providers["openai"], "gpt-6.1-sol"),
        account_label="openai",
        assertion="OpenAI only",
    )
    consent(owner, "openai")
    pools.approve_funding(
        providers,
        "paid",
        by_id=owner.pk,
        budget_usd="100",
        expires_at=END,
        valid_from=START,
        accounts=pools.funding_accounts(providers, providers.pools.get("paid")),
        note="cumulative",
        budget_period=monthly.CUMULATIVE,
        version=0,
    )
    grant = pools.snapshot(providers, owner.pk).get("paid").grant
    reserve(owner, providers, grant, "1")
    reserve(owner, providers, grant, "1", "anthropic/claude-opus-5-5")
    anthropic = InferenceRequest.objects.get(provider="anthropic")
    assert anthropic.vendor == "anthropic" and anthropic.provider_family_id is None
    reserve(owner, providers, grant, "1")  # OpenAI is unaffected
    assert monthly.historical_problem("openai") == ""
    # The vendor's own unmapped history still fails closed.
    assert "Ambiguous historical" in monthly.historical_problem("anthropic")


def test_same_vendor_ambiguous_history_still_fails_closed_before_send(clock):  # noqa: F811
    owner, admin, providers, grant = setup_budget(clock)
    InferenceRequest.objects.create(
        pool=f"grant:{uuid.uuid4()}",
        provider="openai",
        model="gpt-6.1-sol",
        billing_mode="metered",
        vendor="openai",
        reserved_usd=Decimal(1),
        cost_status="pending",
    )
    sent = []
    with pytest.raises(spend_guard.SpendDenied, match="Ambiguous historical"):
        reserve(owner, providers, grant, "1")
    assert sent == []
    with pytest.raises(ValueError, match="Ambiguous historical"):
        consent(owner, version=1)
    # Another vendor is not blocked by it, but unknown-vendor history blocks all.
    InferenceRequest.objects.update(vendor="")
    assert "Ambiguous historical" in monthly.historical_problem("google")
    assert "Ambiguous historical" in monthly.historical_problem("openai")


def test_unbounded_unknown_exposure_is_scoped_per_vendor(clock):  # noqa: F811
    owner, admin, providers, grant = setup_budget(clock)
    InferenceRequest.objects.create(
        pool=f"grant:{uuid.uuid4()}",
        provider="x",
        model="m",
        billing_mode="metered",
        vendor="anthropic",
        cost_status="unknown",
    )
    assert monthly.historical_problem("openai") == ""
    assert "no positive bound" in monthly.historical_problem("anthropic") or (
        "Ambiguous" in monthly.historical_problem("anthropic")
    )


# -- P2-1: funding writes are versioned and kind-stable --------------------------


def fund(providers, owner, **overrides):
    values = {
        "by_id": owner.pk,
        "budget_usd": "100",
        "expires_at": END,
        "valid_from": START,
        "accounts": pools.funding_accounts(providers, providers.pools.get("paid")),
        "note": "extend expiry",
        **overrides,
    }
    return pools.approve_funding(providers, "paid", **values)


def test_funding_write_requires_version_and_period(clock):  # noqa: F811
    owner, _, providers, grant = setup_budget(clock)
    before = SpendFunding.objects.filter(envelope=grant.envelope).count()
    with pytest.raises(TypeError):
        fund(providers, owner)  # neither version nor period
    with pytest.raises(TypeError):
        fund(providers, owner, budget_period=monthly.MONTHLY)
    with pytest.raises(TypeError):
        fund(providers, owner, version=1)
    with pytest.raises(ValueError, match="version"):
        fund(providers, owner, budget_period=monthly.MONTHLY, version=None)
    assert SpendFunding.objects.filter(envelope=grant.envelope).count() == before
    assert pools.snapshot(providers, owner.pk).get("paid").grant.budget_period == (
        monthly.MONTHLY
    )


def test_monthly_cannot_silently_become_cumulative(clock):  # noqa: F811
    owner, admin, providers, grant = setup_budget(clock)
    latest = SpendFunding.objects.filter(envelope=grant.envelope).latest("pk")
    with pytest.raises(ValueError, match="conversion"):
        fund(providers, owner, budget_period=monthly.CUMULATIVE, version=latest.pk)
    assert SpendFunding.objects.filter(envelope=grant.envelope).latest("pk") == latest
    # The same kind, current version extends fine.
    again = fund(providers, owner, budget_period=monthly.MONTHLY, version=latest.pk)
    assert again.budget_period == monthly.MONTHLY


def test_cumulative_cannot_silently_become_monthly(clock):  # noqa: F811
    owner, admin, providers, grant = setup_budget(clock, recurring=False)
    latest = SpendFunding.objects.filter(envelope=grant.envelope).latest("pk")
    with pytest.raises(ValueError, match="conversion"):
        fund(providers, owner, budget_period=monthly.MONTHLY, version=latest.pk)
    assert SpendFunding.objects.filter(envelope=grant.envelope).latest("pk") == latest


def test_explicit_conversion_is_audited_and_still_versioned(clock):  # noqa: F811
    from django_ergo.conversation.models import SpendControlAudit

    owner, admin, providers, grant = setup_budget(clock)
    latest = SpendFunding.objects.filter(envelope=grant.envelope).latest("pk")
    with pytest.raises(pools.VersionConflict):
        fund(
            providers,
            owner,
            budget_period=monthly.CUMULATIVE,
            version=latest.pk - 1,
            convert_period=True,
        )
    converted = fund(
        providers,
        owner,
        budget_period=monthly.CUMULATIVE,
        version=latest.pk,
        convert_period=True,
    )
    assert converted.budget_period == monthly.CUMULATIVE
    audit = SpendControlAudit.objects.filter(action="fund").latest("pk")
    assert audit.details["converted_from"] == monthly.MONTHLY


def test_stale_version_refused_even_for_same_kind(clock):  # noqa: F811
    owner, _, providers, grant = setup_budget(clock)
    with pytest.raises(pools.VersionConflict):
        fund(providers, owner, budget_period=monthly.MONTHLY, version=0)


# -- controls: invariants that hold -------------------------------------------


def test_control_config_ceiling_and_mapping_never_create_consent(clock):  # noqa: F811
    owner = User.objects.create(username="lee")
    admin = User.objects.create(username="admin", is_superuser=True)
    providers = parse_providers(POOLS.replace("budget_usd: 10", "budget_usd: 1000"))
    for ref in pools.pool_refs(providers, providers.pools.get("paid"), metered=True):
        head, _, model = ref.partition("/")
        monthly.assert_mapping(
            actor_id=admin.pk,
            owner_id=owner.pk,
            identity=pools.transport_identity(providers.providers[head], model),
            account_label=head,
            assertion="mapping only",
        )
    pools.approve_funding(
        providers,
        "paid",
        by_id=owner.pk,
        budget_usd="100",
        expires_at=END,
        valid_from=START,
        accounts=pools.funding_accounts(providers, providers.pools.get("paid")),
        budget_period=monthly.MONTHLY,
        version=0,
    )
    grant = pools.snapshot(providers, owner.pk).get("paid").grant
    with pytest.raises(spend_guard.SpendDenied, match="consent is pending"):
        reserve(owner, providers, grant, "1")
    assert not InferenceRequest.objects.exists()


def test_control_consent_is_capped_and_owner_or_admin_only(clock):  # noqa: F811
    owner, admin, providers, grant = setup_budget(clock)
    stranger = User.objects.create(username="stranger")
    with pytest.raises(PermissionError):
        consent(stranger, version=1)
    with pytest.raises(ValueError, match="at most"):
        consent(owner, amount="100.0001", version=1)
    with pytest.raises(ValueError, match="UTC"):
        monthly.set_consent(
            actor_id=owner.pk,
            family_id="openai",
            version=1,
            monthly_usd="100",
            enabled=True,
            valid_from=START,
            expires_at=END,
            note="x",
            timezone_name="America/Los_Angeles",
        )


def test_control_toggle_off_denies_prebuilt_guard_before_transport(clock):  # noqa: F811
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
    consent(owner, enabled=False, version=1)
    sent = []

    async def send():
        sent.append(1)
        return SimpleNamespace(id="x", usage=None)

    with pytest.raises(spend_guard.SpendDenied, match="disabled"):
        async_to_sync(guard.call)(
            {"model": "gpt-6.1-sol", "messages": [], "max_tokens": 10},
            spend_guard.OpenAIChatAdapter(),
            send,
        )
    assert sent == []
    assert not InferenceRequest.objects.filter(provider_family="openai").exists()


def test_control_unknown_hold_survives_two_rollovers_and_settles_to_origin(clock):  # noqa: F811
    owner, _, providers, grant = setup_budget(clock)
    hold = reserve(owner, providers, grant, "60")  # 2026-11-30T23:59:59Z
    clock[0] += dt.timedelta(seconds=2)  # December
    with pytest.raises(spend_guard.SpendDenied, match="monthly budget"):
        reserve(owner, providers, grant, "41")
    clock[0] += dt.timedelta(days=32)  # January
    with pytest.raises(spend_guard.SpendDenied, match="monthly budget"):
        reserve(owner, providers, grant, "41")
    settle(hold, "60")
    assert InferenceRequest.objects.get(pk=hold).budget_month == dt.date(2026, 11, 1)
    reserve(owner, providers, grant, "100")  # January is whole again, not doubled


def test_control_rollover_reservation_cannot_double_count(clock):  # noqa: F811
    owner, _, providers, grant = setup_budget(clock)
    nov = reserve(owner, providers, grant, "100")
    clock[0] += dt.timedelta(seconds=2)
    with pytest.raises(spend_guard.SpendDenied):
        reserve(owner, providers, grant, "0.0001")  # pending Nov hold still counts
    settle(nov, "100")
    reserve(owner, providers, grant, "100")  # December fully available once
    with pytest.raises(spend_guard.SpendDenied):
        reserve(owner, providers, grant, "0.0001")


def test_control_two_pools_two_owners_share_one_vendor_bucket_under_race(clock):  # noqa: F811
    text = POOLS.replace("budget_usd: 10", "budget_usd: 100").replace(
        "private_pools:",
        "  - name: paid2\n    owner: bob\n    routes: [openai]\n    budget_usd: 100\n"
        "    expires: 2099-12-31\nprivate_pools:",
    )
    owner, admin, providers, grant = setup_budget(clock, text=text)
    bob = User.objects.create(username="bob")
    # Admin's explicit "fund separately" decision: a second, independent envelope.
    pools.link_config_envelope(
        providers, "paid2", by_id=admin.pk, admin=True, fund=True, note="separate"
    )
    pools.approve_funding(
        providers,
        "paid2",
        by_id=bob.pk,
        budget_usd="100",
        expires_at=END,
        valid_from=START,
        accounts=pools.funding_accounts(providers, providers.pools.get("paid2")),
        budget_period=monthly.MONTHLY,
        version=0,
    )
    grant2 = pools.snapshot(providers, bob.pk).get("paid2").grant
    assert grant2.envelope_id != grant.envelope_id
    jobs = [(owner, grant), (bob, grant2)] * 4
    barrier = threading.Barrier(len(jobs))

    def attempt(job):
        user, row = job
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            reserve(user, providers, row, "20")
            return "reserved"
        except spend_guard.SpendDenied:
            return "denied"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
        results = list(executor.map(attempt, jobs))
    assert results.count("reserved") == 5
    assert monthly.inspect_family("openai")["reserved_usd"] == "100.0000000000"


def test_control_unmapped_second_key_on_enrolled_vendor_denied(clock):  # noqa: F811
    text = (
        POOLS.replace("budget_usd: 10", "budget_usd: 100")
        .replace(
            "  anthropic:\n",
            "  second:\n    type: openai\n    api_key_env: SECOND_SYNTHETIC_KEY\n"
            "    models: [{name: gpt-6.1-sol, price: {input: 1, output: 10}}]\n"
            "  anthropic:\n",
        )
        .replace("routes: [openai, anthropic]", "routes: [openai, second, anthropic]")
    )
    owner, admin, providers, grant = setup_budget(clock)  # maps only the first key
    altered = parse_providers(text)
    with pytest.raises(spend_guard.SpendDenied):
        reserve(owner, altered, grant, "1", "second/gpt-6.1-sol")
    assert not InferenceRequest.objects.filter(provider="second").exists()


def test_control_paid_pause_denies_recurring_before_transport(clock):  # noqa: F811
    from django_ergo.conversation.models import PaidDispatchState

    owner, admin, providers, grant = setup_budget(clock)
    state = PaidDispatchState.objects.get(pk=1)
    pools.set_paid_pause(
        by_id=admin.pk, paused=True, version=state.updated_at.isoformat()
    )
    with pytest.raises(spend_guard.SpendDenied):
        reserve(owner, providers, grant, "1")
    assert not InferenceRequest.objects.exists()


# -- P2-2: the vendor bucket follows the audited mapping, not the wire type -----

WIRE = """
default: auto/medium
providers:
  gemini:
    type: google
    api_key_env: TEST_GEMINI_KEY
    models:
      - {name: gemini-2.5-pro, price: {input: 1.25, cached: 0.125, output: 10}}
  gemini-compat:
    type: openai
    api_key_env: TEST_GEMINI_KEY
    config: {base_url: "https://generativelanguage.googleapis.com/v1beta/openai"}
    models:
      - {name: gemini-2.5-pro, price: {input: 1.25, cached: 0.125, output: 10}}
  claude:
    type: claude
    transport: cli
    models: [claude-opus-5-5]
tiers:
  medium: [claude/claude-opus-5-5]
agents:
  medium:
    - {agent: claude, model: claude-opus-5-5, provider: claude}
pools:
  - name: subs
    routes: [claude]
  - name: paid
    owner: lee
    routes: [gemini, gemini-compat]
    budget_usd: 300
    expires: 2099-12-31
"""


def map_wire_accounts(providers, owner, admin, *, compat_family=""):
    for name, family in (("gemini", ""), ("gemini-compat", compat_family)):
        monthly.assert_mapping(
            actor_id=admin.pk,
            owner_id=owner.pk,
            identity=pools.transport_identity(
                providers.providers[name], "gemini-2.5-pro"
            ),
            account_label="Google billing",
            assertion="This is the Google billing account",
            family=family,
        )


def test_openai_wire_to_google_cannot_default_into_openai_family(clock, monkeypatch):  # noqa: F811
    monkeypatch.setenv("TEST_GEMINI_KEY", "synthetic")
    owner = User.objects.create(username="lee")
    admin = User.objects.create(username="admin", is_superuser=True)
    providers = parse_providers(WIRE)
    with pytest.raises(ValueError, match="billed by google"):
        map_wire_accounts(providers, owner, admin)  # wire default would be openai
    with pytest.raises(ValueError, match="billed by google"):
        map_wire_accounts(providers, owner, admin, compat_family="openai")
    assert not ProviderAccountMapping.objects.filter(
        identity__contains="generativelanguage.googleapis.com/v1beta/openai"
    ).exists()


def test_one_google_account_has_one_cap_via_openai_wire(clock, monkeypatch):  # noqa: F811
    monkeypatch.setenv("TEST_GEMINI_KEY", "synthetic")
    owner = User.objects.create(username="lee")
    admin = User.objects.create(username="admin", is_superuser=True)
    providers = parse_providers(WIRE)
    map_wire_accounts(providers, owner, admin, compat_family="google")
    assert [f.pk for f in ProviderBudgetFamily.objects.all()] == ["google"]
    consent(owner, "google")
    pools.approve_funding(
        providers,
        "paid",
        by_id=owner.pk,
        budget_usd="300",
        expires_at=END,
        valid_from=START,
        accounts=pools.funding_accounts(providers, providers.pools.get("paid")),
        budget_period=monthly.MONTHLY,
        version=0,
    )
    grant = pools.snapshot(providers, owner.pk).get("paid").grant
    reserve(owner, providers, grant, "60", "gemini/gemini-2.5-pro")
    reserve(owner, providers, grant, "40", "gemini-compat/gemini-2.5-pro")
    before = InferenceRequest.objects.count()
    for ref in ("gemini/gemini-2.5-pro", "gemini-compat/gemini-2.5-pro"):
        with pytest.raises(spend_guard.SpendDenied, match="monthly budget"):
            reserve(owner, providers, grant, "0.0001", ref)
    assert InferenceRequest.objects.count() == before  # denied before any send row
    assert monthly.inspect_family("google")["reserved_usd"] == "100.0000000000"
    assert {r.provider_family_id for r in InferenceRequest.objects.all()} == {"google"}


def test_unmapped_alias_of_a_mapped_credential_gets_no_second_cap(clock, monkeypatch):  # noqa: F811
    monkeypatch.setenv("TEST_GEMINI_KEY", "synthetic")
    owner = User.objects.create(username="lee")
    admin = User.objects.create(username="admin", is_superuser=True)
    providers = parse_providers(WIRE)
    monthly.assert_mapping(
        actor_id=admin.pk,
        owner_id=owner.pk,
        identity=pools.transport_identity(
            providers.providers["gemini"], "gemini-2.5-pro"
        ),
        account_label="Google billing",
        assertion="Google",
    )
    compat = pools.transport_identity(
        providers.providers["gemini-compat"], "gemini-2.5-pro"
    )
    # Same credential source and account: it is the Google account, never "openai".
    assert monthly.family_of(compat) == "google"
    with pytest.raises(ValueError, match="billed by google"):
        monthly.assert_mapping(
            actor_id=admin.pk,
            owner_id=owner.pk,
            identity=compat,
            account_label="x",
            assertion="x",
            family="openai",
        )
    consent(owner, "google")
    pools.approve_funding(
        providers,
        "paid",
        by_id=owner.pk,
        budget_usd="300",
        expires_at=END,
        valid_from=START,
        accounts=[
            pools.transport_identity(providers.providers["gemini"], "gemini-2.5-pro")
        ],
        budget_period=monthly.MONTHLY,
        version=0,
    )
    grant = pools.snapshot(providers, owner.pk).get("paid").grant
    with pytest.raises(spend_guard.SpendDenied):
        reserve(owner, providers, grant, "1", "gemini-compat/gemini-2.5-pro")
    assert not InferenceRequest.objects.filter(provider="gemini-compat").exists()


def test_other_vendor_cannot_use_openai_wire_to_join_a_family(clock, monkeypatch):  # noqa: F811
    monkeypatch.setenv("TEST_GEMINI_KEY", "synthetic")
    owner = User.objects.create(username="lee")
    admin = User.objects.create(username="admin", is_superuser=True)
    text = WIRE.replace(
        "https://generativelanguage.googleapis.com/v1beta/openai", "https://api.x.ai/v1"
    )
    providers = parse_providers(text)
    for family in ("", "openai", "google"):
        with pytest.raises(ValueError, match="first-party"):
            monthly.assert_mapping(
                actor_id=admin.pk,
                owner_id=owner.pk,
                identity=pools.transport_identity(
                    providers.providers["gemini-compat"], "gemini-2.5-pro"
                ),
                account_label="xai",
                assertion="xAI",
                family=family,
            )


def test_native_wire_cannot_be_asserted_into_another_vendor(clock, monkeypatch):  # noqa: F811
    monkeypatch.setenv("TEST_GEMINI_KEY", "synthetic")
    owner = User.objects.create(username="lee")
    admin = User.objects.create(username="admin", is_superuser=True)
    providers = parse_providers(WIRE)
    with pytest.raises(ValueError, match="belongs to the google family"):
        monthly.assert_mapping(
            actor_id=admin.pk,
            owner_id=owner.pk,
            identity=pools.transport_identity(
                providers.providers["gemini"], "gemini-2.5-pro"
            ),
            account_label="x",
            assertion="x",
            family="openai",
        )


@pytest.mark.django_db(transaction=True)
def test_control_migration_main_leaf_0063_to_0064_and_back(settings):
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    settings.USE_TZ = True
    main = ("django_ergo", "0063_google_engine_signatures")
    leaf = ("django_ergo", "0064_provider_monthly_budgets")
    executor = MigrationExecutor(connection)
    assert [n for n in executor.loader.graph.leaf_nodes() if n[0] == "django_ergo"] == [
        leaf
    ]
    assert main in {p.key for p in executor.loader.graph.node_map[leaf].parents}
    try:
        executor.migrate([main])
        old = executor.loader.project_state([main]).apps
        attempt_model = old.get_model("django_ergo", "InferenceRequest")
        priced = attempt_model.objects.create(
            provider="openai",
            model="m",
            billing_mode="metered",
            reserved_usd=Decimal(5),
            cost_usd=Decimal("4.5"),
            cost_status="priced",
        )
        attempt_model.objects.filter(pk=priced.pk).update(
            created_at=dt.datetime(2026, 11, 30, 23, 59, 59, 999999, tzinfo=dt.UTC)
        )
        executor = MigrationExecutor(connection)
        executor.migrate([leaf])
        new = executor.loader.project_state([leaf]).apps
        row = new.get_model("django_ergo", "InferenceRequest").objects.get(pk=priced.pk)
        assert row.budget_month == dt.date(2026, 11, 1)  # original month, UTC
        assert row.cost_usd == Decimal("4.5") and row.reserved_usd == Decimal(5)
        assert not new.get_model("django_ergo", "ProviderBudgetFamily").objects.exists()
        assert not new.get_model(
            "django_ergo", "ProviderMonthlyConsent"
        ).objects.exists()
        assert not new.get_model(
            "django_ergo", "ProviderAccountMapping"
        ).objects.exists()
        assert monthly.historical_problem("openai")  # fails closed until reconciled
        MigrationExecutor(connection).migrate([main])
        back = old.get_model("django_ergo", "InferenceRequest").objects.get(
            pk=priced.pk
        )
        assert back.cost_usd == Decimal("4.5")
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


def test_one_credential_source_cannot_join_two_families(clock, monkeypatch):  # noqa: F811
    """The same key env on a first-party OpenAI endpoint and on Google's endpoint
    is one credential: it can't be asserted into both vendors' caps."""
    monkeypatch.setenv("TEST_GEMINI_KEY", "synthetic")
    owner = User.objects.create(username="lee")
    admin = User.objects.create(username="admin", is_superuser=True)
    text = WIRE.replace(
        '    config: {base_url: "https://generativelanguage.googleapis.com/v1beta/openai"}\n',
        "",
    )
    providers = parse_providers(text)
    monthly.assert_mapping(
        actor_id=admin.pk,
        owner_id=owner.pk,
        identity=pools.transport_identity(
            providers.providers["gemini"], "gemini-2.5-pro"
        ),
        account_label="Google",
        assertion="Google",
    )
    with pytest.raises(ValueError, match="already mapped to the google family"):
        monthly.assert_mapping(
            actor_id=admin.pk,
            owner_id=owner.pk,
            identity=pools.transport_identity(
                providers.providers["gemini-compat"], "gemini-2.5-pro"
            ),
            account_label="Google",
            assertion="Google",
        )
    assert [f.pk for f in ProviderBudgetFamily.objects.all()] == ["google"]
