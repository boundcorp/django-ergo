"""The merged #407 authority with real coding Decisions, mocked inference only."""

import datetime as dt
from decimal import Decimal

import httpx
import pytest

from django_ergo.bots import pools
from django_ergo.conversation import spend_guard
from django_ergo.conversation import weekly_budgets as weekly
from django_ergo.conversation.models import InferenceRequest
from django_ergo.conversation.paid_agent_proxy import Proxy
from django_ergo.conversation.paid_agent_proxy import issue
from django_ergo.conversation.paid_agent_proxy import mapped_account
from tests.test_model_pools import POOLS
from tests.test_model_pools import chat
from tests.test_model_pools import environment as environment_fixture
from tests.test_model_pools import registry_of
from tests.test_model_pools import set_pref
from tests.test_paid_agent_proxy import payload
from tests.test_paid_agent_proxy import receipt
from tests.test_weekly_provider_budgets import clock as clock_fixture
from tests.test_weekly_provider_budgets import consent
from tests.test_weekly_provider_budgets import setup_budget

clock = clock_fixture
environment = environment_fixture

pytestmark = pytest.mark.django_db(transaction=True)


def launch(clock):
    text = POOLS.replace("budget_usd: 10", "budget_usd: 100")
    owner, _, providers, _ = setup_budget(clock, text=text, amount="25")
    consent(owner, "openai", amount="25", version=1)
    _, bots = registry_of(text)
    bot = bots["devbox"]
    session = chat(bot, owner)
    set_pref(owner.pk, providers, active_pool="paid")
    decision = pools.select_agent(
        bot,
        session,
        model="openai/gpt-6.1-sol",
        agent="codex",
        manager_agents=("codex",),
        confinable=True,
        spend_bounded=True,
    )
    guard = decision.guard(bot, session, purpose="paid_agent")
    account = mapped_account(decision.transport_identity)
    row, token = issue(
        guard=guard,
        reservation_id=decision.reservation_id,
        account_ref=account,
        authority={},
    )
    return owner, bot, session, decision, guard, account, row, token


def test_real_coding_decision_and_chat_share_weekly_cap(clock):
    owner, bot, session, decision, guard, account, row, token = launch(clock)
    allocation = InferenceRequest.objects.get(pk=decision.reservation_id)
    assert row.budget_week == allocation.budget_week == weekly.week_of()
    assert str(allocation.account_mapping_id) == account
    assert allocation.outcome == "not_sent"
    proxy = Proxy(
        lambda _: (guard, "FAKE mock key", account),
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=receipt()))
        ),
    )
    proxy.request(token, "responses", payload())
    attempt = row.requests.get()
    assert attempt.account_mapping_id == allocation.account_mapping_id
    assert weekly.inspect_family("openai")["spent_usd"] == str(attempt.cost_usd)
    # A chat reservation consumes the same pool and provider-family authority.
    spend_guard.reserve(
        grant_id=decision.grant_id,
        provider="openai",
        model=decision.model,
        amount=Decimal("24.99"),
        user_id=owner.pk,
        current=guard.current,
        fields={},
    )
    with pytest.raises(spend_guard.SpendDenied, match="budget"):
        spend_guard.reserve_launch(
            grant_id=decision.grant_id,
            pool="paid",
            provider="openai",
            model=decision.model,
            amount=Decimal(1),
            user_id=owner.pk,
            current=guard.current,
            proxy_bound=True,
        )


def test_sunday_proxy_attempt_settles_monday_and_unknown_carries(clock):
    owner, bot, session, decision, guard, account, row, token = launch(clock)
    proxy = Proxy(
        lambda _: (guard, "FAKE mock key", account),
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=receipt()))
        ),
    )
    _, pk, adapter, price, _ = proxy.begin(token, "responses", payload())
    old_week = row.budget_week
    clock[0] += dt.timedelta(seconds=2)
    assert weekly.week_of() != old_week
    assert Decimal(weekly.inspect_family("openai")["reserved_usd"]) > 0
    with pytest.raises(spend_guard.SpendDenied, match="earlier UTC"):
        proxy.request(token, "responses", payload())
    proxy.settle(row, pk, adapter, price, "responses", receipt(), True)
    attempt = InferenceRequest.objects.get(pk=pk)
    assert attempt.budget_week == old_week and attempt.cost_status == "priced"
    assert Decimal(weekly.inspect_family("openai")["spent_usd"]) == 0
    assert Decimal(weekly.inspect_family("openai")["reserved_usd"]) == 0
