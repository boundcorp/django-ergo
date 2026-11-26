"""P2-R1: a vendor is stamped only when the audited mapping or the host proves it.

Synthetic keys, mocked transports and a synthetic UTC clock only.
"""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from django_ergo.bots import pools
from django_ergo.bots.providers import parse_providers
from django_ergo.conversation import monthly_budgets as monthly
from django_ergo.conversation import spend_guard
from django_ergo.conversation.models import InferenceRequest
from tests.test_model_pools import environment  # noqa: F401 -- synthetic keys only
from tests.test_monthly_provider_budgets import END
from tests.test_monthly_provider_budgets import START
from tests.test_monthly_provider_budgets import clock  # noqa: F401
from tests.test_monthly_provider_budgets import consent
from tests.test_monthly_provider_budgets import reserve

pytestmark = pytest.mark.django_db(transaction=True)
User = get_user_model()

CONFIG = """
default: auto/medium
providers:
  gemini:
    type: google
    api_key_env: TEST_GEMINI_KEY
    models:
      - {name: gemini-2.5-pro, price: {input: 1.25, cached: 0.125, output: 10}}
  gemini-compat:
    type: openai
    api_key_env: TEST_GOOGLE_KEY_2
    config: {base_url: "https://generativelanguage.googleapis.com/v1beta/openai"}
    models:
      - {name: gemini-2.5-pro, price: {input: 1.25, cached: 0.125, output: 10}}
  proxy:
    type: openai
    api_key_env: TEST_PROXY_KEY
    config: {base_url: "https://llm-proxy.example/v1"}
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
    routes: [gemini, gemini-compat, proxy]
    budget_usd: 300
    expires: 2099-12-31
"""


def _setup(monkeypatch):
    for key in ("TEST_GEMINI_KEY", "TEST_GOOGLE_KEY_2", "TEST_PROXY_KEY"):
        monkeypatch.setenv(key, "synthetic")
    owner = User.objects.create(username="lee")
    admin = User.objects.create(username="admin", is_superuser=True)
    providers = parse_providers(CONFIG)
    monthly.assert_mapping(
        actor_id=admin.pk,
        owner_id=owner.pk,
        identity=pools.transport_identity(
            providers.providers["gemini"], "gemini-2.5-pro"
        ),
        account_label="Google billing",
        assertion="The Google billing account",
    )
    consent(owner, "google")
    pools.approve_funding(
        providers,
        "paid",
        by_id=owner.pk,
        budget_usd="300",
        expires_at=END,
        valid_from=START,
        accounts=pools.funding_accounts(providers, providers.pools.get("paid")),
        budget_period=monthly.CUMULATIVE,
        version=0,
    )
    grant = pools.snapshot(providers, owner.pk).get("paid").grant
    return owner, providers, grant


def test_google_host_on_openai_wire_is_google_and_needs_a_mapping(clock, monkeypatch):  # noqa: F811
    owner, providers, grant = _setup(monkeypatch)
    identity = pools.transport_identity(
        providers.providers["gemini-compat"], "gemini-2.5-pro"
    )
    assert monthly.family_of(identity) == "google"
    # Google is enrolled but this credential is unmapped: denied before any row.
    with pytest.raises(spend_guard.SpendDenied, match="explicit admin mapping"):
        reserve(owner, providers, grant, "40", "gemini-compat/gemini-2.5-pro")
    assert not InferenceRequest.objects.exists()


def test_unprovable_vendor_is_unknown_and_blocks_google(clock, monkeypatch):  # noqa: F811
    owner, providers, grant = _setup(monkeypatch)
    identity = pools.transport_identity(providers.providers["proxy"], "gemini-2.5-pro")
    assert monthly.family_of(identity) == ""
    pk = reserve(owner, providers, grant, "40", "proxy/gemini-2.5-pro")
    row = InferenceRequest.objects.get(pk=pk)
    assert row.vendor == ""  # never the wire type 'openai'
    assert row.provider_family_id is None
    # Unknown history counts in the conservative gate: Google fails closed.
    assert "Ambiguous historical" in monthly.historical_problem("google")
    with pytest.raises(spend_guard.SpendDenied, match="Ambiguous historical"):
        reserve(owner, providers, grant, "10", "gemini/gemini-2.5-pro")
    assert sum(r.reserved_usd for r in InferenceRequest.objects.all()) == Decimal(40)


def test_unknown_vendor_fails_closed_for_recurring_sends(clock, monkeypatch):  # noqa: F811
    owner, providers, grant = _setup(monkeypatch)
    identity = pools.transport_identity(providers.providers["proxy"], "gemini-2.5-pro")
    with pytest.raises(spend_guard.SpendDenied, match="mapping"):
        monthly.reservation_context(identity, Decimal(1), required=True)
