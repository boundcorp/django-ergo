"""Google engine under explicit funding, privacy fences and the paid pause latch.

Mocked HTTP only; every key and account is synthetic.
"""

import httpx
import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model

from django_ergo.bots import pools
from django_ergo.conversation import spend_guard
from django_ergo.conversation.engines import google_api
from tests.pool_funding import fund
from tests.test_conversation_google_spend import POOLS
from tests.test_conversation_google_spend import reply
from tests.test_model_pools import chat
from tests.test_model_pools import (
    environment,  # noqa: F401 -- synthetic keys/CLIs for this module
)
from tests.test_model_pools_authority import _process

User = get_user_model()
MESSAGES = [{"role": "user", "content": "hello"}]


def _built(tmp_path, monkeypatch, *, funded=True):
    monkeypatch.setenv("TEST_GEMINI_KEY", "fake-gemini-key")
    user = User.objects.create(username="lee")
    path = tmp_path / "providers.yaml"
    path.write_text(POOLS)
    _, bot = _process(tmp_path)
    if funded:
        fund(bot.providers, user)
    session = chat(bot, user)
    pools.update_preference(user.pk, bot.providers, version=0, active_pool="paid")
    requests = []

    def send(request):
        requests.append(request)
        return httpx.Response(200, json=reply())

    monkeypatch.setattr(google_api, "_transport", lambda: httpx.MockTransport(send))
    # Built the way Bot.engine_spec builds it, including the pool account.
    account = bot.providers.find("gemini/gemini-2.5-pro")[0].account
    engine = google_api.GoogleAPIEngine(
        {
            "model": "gemini-2.5-pro",
            "api_key_env": "TEST_GEMINI_KEY",
            "_pool_account": account,
        }
    )
    return bot, user, session, path, engine, requests


def _attach(bot, session, engine):
    decision = pools.select_route(bot, session, ref="gemini/gemini-2.5-pro")
    engine.spend_guard = decision.guard(bot, session)


@pytest.mark.django_db(transaction=True)
def test_funded_google_engine_sends_once(tmp_path, monkeypatch):
    bot, _, session, _, engine, requests = _built(tmp_path, monkeypatch)
    _attach(bot, session, engine)
    async_to_sync(engine.complete)(MESSAGES)
    assert len(requests) == 1


@pytest.mark.django_db(transaction=True)
def test_config_ceiling_without_explicit_funding_refuses_google(
    tmp_path, monkeypatch
):  # C1
    bot, _, session, _, engine, requests = _built(tmp_path, monkeypatch, funded=False)
    with pytest.raises(pools.PoolRefusal):
        _attach(bot, session, engine)
    # An engine with no guard at all must not slip through once pools are active.
    with pytest.raises(spend_guard.SpendDenied):
        async_to_sync(engine.complete)(MESSAGES)
    assert requests == []


@pytest.mark.django_db(transaction=True)
def test_privacy_restriction_added_after_build_refuses_google(
    tmp_path, monkeypatch
):  # C2
    bot, _, session, path, engine, requests = _built(tmp_path, monkeypatch)
    _attach(bot, session, engine)
    path.write_text(POOLS + "\nprivacy:\n  work:\n    bots: [devbox]\n    allow: []\n")
    with pytest.raises(spend_guard.SpendDenied, match="privacy"):
        async_to_sync(engine.complete)(MESSAGES)
    assert requests == []


@pytest.mark.django_db(transaction=True)
def test_paused_latch_refuses_google_before_send(tmp_path, monkeypatch):  # C4
    bot, _, session, _, engine, requests = _built(tmp_path, monkeypatch)
    _attach(bot, session, engine)
    admin = User.objects.create(username="admin", is_superuser=True)
    pools.set_paid_pause(by_id=admin.pk, paused=True)
    with pytest.raises(spend_guard.SpendDenied, match="paused"):
        async_to_sync(engine.complete)(MESSAGES)
    assert requests == []
    # The latch is durable but lifts: the same built engine works again.
    pools.set_paid_pause(by_id=admin.pk, paused=False)
    async_to_sync(engine.complete)(MESSAGES)
    assert len(requests) == 1


@pytest.mark.django_db(transaction=True)
def test_unguarded_google_engine_refuses_while_paused(tmp_path, monkeypatch):  # C4
    _, _, _, _, engine, requests = _built(tmp_path, monkeypatch)
    admin = User.objects.create(username="admin", is_superuser=True)
    pools.set_paid_pause(by_id=admin.pk, paused=True)
    with pytest.raises(spend_guard.SpendDenied):
        async_to_sync(engine.complete)(MESSAGES)
    assert requests == []


@pytest.mark.django_db(transaction=True)
def test_engine_built_for_one_endpoint_refuses_after_base_url_change(
    tmp_path, monkeypatch
):  # C3
    bot, _, session, _, engine, requests = _built(tmp_path, monkeypatch)
    _attach(bot, session, engine)
    engine.base_url = "https://example.invalid/v1beta"
    with pytest.raises(spend_guard.SpendDenied, match="transport/account"):
        async_to_sync(engine.complete)(MESSAGES)
    assert requests == []
