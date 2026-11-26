"""Mock-only acceptance for the final pool re-review."""

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model

from django_ergo.bots import pools
from django_ergo.conversation import spend_guard
from django_ergo.conversation.models import PaidDispatchState
from django_ergo.conversation.models import SpendGrant
from tests.pool_funding import fund
from tests.test_model_pools import POOLS
from tests.test_model_pools import environment  # noqa: F401
from tests.test_model_pools_authority import _process
from tests.test_model_pools_rereview import _guard

User = get_user_model()


@pytest.mark.django_db
def test_decisionless_authority_refuses(tmp_path):
    user = User.objects.create(username="lee")
    (tmp_path / "providers.yaml").write_text(POOLS)
    _, bot = _process(tmp_path)
    fund(bot.providers, user)
    guard = _guard(bot, user)
    assert isinstance(
        pools.SendAuthority(bot)(SpendGrant.objects.get(pk=guard.grant_id)), str
    )


@pytest.mark.django_db(transaction=True)
def test_pause_version_refuses_stale_and_missing():
    a = User.objects.create(username="a", is_superuser=True)
    b = User.objects.create(username="b", is_superuser=True)
    state = pools.set_paid_pause(by_id=a.pk, paused=True, version="")
    stale = state.updated_at.isoformat()
    state = pools.set_paid_pause(by_id=b.pk, paused=False, version=stale)
    pools.set_paid_pause(by_id=b.pk, paused=True, version=state.updated_at.isoformat())
    with pytest.raises(pools.VersionConflict):
        pools.set_paid_pause(by_id=a.pk, paused=False, version=stale)
    with pytest.raises(TypeError):
        pools.set_paid_pause(by_id=a.pk, paused=False)
    assert PaidDispatchState.objects.get(pk=1).paused


@pytest.mark.django_db(transaction=True)
def test_paused_transcription_refuses_before_client(monkeypatch):
    import openai

    from django_ergo.conversation.attachments import openai_transcription

    admin = User.objects.create(username="root", is_superuser=True)
    pools.set_paid_pause(by_id=admin.pk, paused=True, version="")
    calls = []
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: calls.append(kw))
    with pytest.raises(spend_guard.SpendDenied, match="paused"):
        async_to_sync(openai_transcription)(b"x", "audio/wav", "a.wav")
    assert not calls


@pytest.mark.django_db(transaction=True)
def test_direct_adapters_fenced_after_activation(monkeypatch):
    from django.utils import timezone

    from django_ergo.conversation import video
    from django_ergo.conversation.engines.fireworks_api import FireworksAPIEngine
    from django_ergo.plugins.web_search import WebSearchError
    from django_ergo.plugins.web_search import WebSearchPlugin

    PaidDispatchState.objects.create(pk=1, activated_at=timezone.now())
    calls = []
    monkeypatch.setattr(
        video.urllib.request, "urlopen", lambda *a, **k: calls.append(1)
    )
    with pytest.raises(spend_guard.SpendDenied, match="activated"):
        video.Gemini("fake")._request("GET", "https://example.invalid")
    with pytest.raises(WebSearchError, match="activated"):
        WebSearchPlugin.__new__(WebSearchPlugin)._search_anthropic("test", 1)
    with pytest.raises(spend_guard.SpendDenied, match="activated"):
        async_to_sync(FireworksAPIEngine._create_completion)(object(), {})
    assert not calls


@pytest.mark.django_db(transaction=True)
def test_legacy_unactivated_dispatch_remains_permitted():
    assert not spend_guard.paid_dispatch_problem(legacy=True)
    async_to_sync(spend_guard.check_unguarded_dispatch)()


@pytest.mark.django_db(transaction=True)
def test_video_legacy_and_pause_boundaries(monkeypatch):
    from django_ergo.conversation import video

    calls = []
    monkeypatch.setattr(
        video.urllib.request, "urlopen", lambda *a, **kw: calls.append(1)
    )
    client = video.Gemini("fake")
    client._request("GET", "https://example.invalid")
    assert calls == [1]
    admin = User.objects.create(username="admin", is_superuser=True)
    pools.set_paid_pause(by_id=admin.pk, paused=True, version="")
    with pytest.raises(spend_guard.SpendDenied, match="paused"):
        client._request("GET", "https://example.invalid")
    assert calls == [1]


@pytest.mark.django_db
def test_coding_reservation_rechecks_captured_identity_and_privacy(
    tmp_path, monkeypatch
):
    from tests.test_model_pools import chat

    user = User.objects.create(username="lee")
    path = tmp_path / "providers.yaml"
    path.write_text(POOLS)
    _, bot = _process(tmp_path)
    fund(bot.providers, user)
    session = chat(bot, user, pool="paid")
    calls = []

    def intercept(**kwargs):
        path.write_text(
            POOLS.replace("TEST_POOL_OPENAI_KEY", "OTHER_KEY").replace(
                "TEST_POOL_ANTHROPIC_KEY", "OTHER_ANTHROPIC_KEY"
            )
            + "\nprivacy:\n  work:\n    bots: [devbox]\n    allow: []\n"
        )
        grant = SpendGrant.objects.get(pk=kwargs["grant_id"])
        calls.append(kwargs["current"](grant))
        raise spend_guard.SpendDenied(str(calls[-1]))

    monkeypatch.setattr(spend_guard, "reserve_launch", intercept)
    with pytest.raises(spend_guard.SpendDenied):
        pools.select_agent(
            bot, session, tier="medium", confinable=True, spend_bounded=True
        )
    assert len(calls) == 1 and isinstance(calls[0], str)
