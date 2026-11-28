"""Independent PR #418 selection/accounting controls retained for the OpenAI subset.

Fake keys only: every credential below is a synthetic string, every HTTP call
goes to an ``httpx.MockTransport`` and every child is a local Python process.
``test_repro_*`` PASSING means the flagged behaviour reproduces at this head;
``test_control_*`` confirm a claim of the PR and must keep passing.
"""

import asyncio
import threading
from decimal import Decimal

import httpx
import openai
import pytest
from django.test import override_settings

from django_ergo.conversation.engines.openai_api import OpenAIAPIEngine
from django_ergo.embedding_providers import OpenAIEmbeddingProvider
from django_ergo.openai_credentials import voice_openai_key
from tests.test_model_pools import environment  # noqa: F401 -- synthetic keys only
from tests.test_weekly_provider_budgets import clock  # noqa: F401

AMBIENT = "fake-ambient-openai-key"


def _embedding_response(request):
    return httpx.Response(
        200,
        json={
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.0] * 1536}],
            "model": "text-embedding-3-small",
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        },
    )


def test_control_chat_engine_missing_selector_fails_closed_despite_ambient(
    monkeypatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", AMBIENT)
    monkeypatch.delenv("REVIEW_ORG_KEY", raising=False)
    with pytest.raises(ValueError, match="REVIEW_ORG_KEY"):
        OpenAIAPIEngine({"api_key_env": "REVIEW_ORG_KEY", "model": "gpt-6-luna"})
    # Adversarial: an inline key does not rescue a configured-but-missing name,
    # and an empty value counts as missing.
    monkeypatch.setenv("REVIEW_ORG_KEY", "")
    with pytest.raises(ValueError, match="REVIEW_ORG_KEY"):
        OpenAIAPIEngine(
            {"api_key_env": "REVIEW_ORG_KEY", "api_key": "fake-inline", "model": "m"}
        )


def test_control_chat_engine_sends_only_selected_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", AMBIENT)
    monkeypatch.setenv("REVIEW_ORG_KEY", "fake-org-key")
    engine = OpenAIAPIEngine({"api_key_env": "REVIEW_ORG_KEY", "model": "gpt-6-luna"})
    assert engine.client.api_key == "fake-org-key"
    # Unconfigured legacy behaviour: the SDK default (OPENAI_API_KEY) remains.
    legacy = OpenAIAPIEngine({"model": "gpt-6-luna"})
    assert legacy.client.api_key == AMBIENT


def test_control_embedding_missing_selector_fails_closed_despite_ambient(
    monkeypatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", AMBIENT)
    monkeypatch.delenv("REVIEW_EMBED_KEY", raising=False)
    with pytest.raises(ValueError, match="REVIEW_EMBED_KEY"):
        OpenAIEmbeddingProvider({"api_key_env": "REVIEW_EMBED_KEY"})
    with pytest.raises(ValueError, match="REVIEW_EMBED_KEY"):
        OpenAIEmbeddingProvider(
            {"api_key_env": "REVIEW_EMBED_KEY", "api_key": "fake-inline"}
        )
    # Unconfigured legacy: inline wins, then OPENAI_API_KEY.
    assert OpenAIEmbeddingProvider({"api_key": "fake-inline"}).api_key == "fake-inline"
    assert OpenAIEmbeddingProvider({}).api_key == AMBIENT


def test_control_voice_selector_fails_closed_and_transcription_sends_selected_key(
    monkeypatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", AMBIENT)
    monkeypatch.delenv("REVIEW_VOICE_KEY", raising=False)
    seen = []

    def handler(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"text": "hello"})

    real = openai.AsyncOpenAI

    class Mocked(real):
        def __init__(self, **kwargs):
            kwargs["http_client"] = httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            )
            super().__init__(**kwargs)

    monkeypatch.setattr(openai, "AsyncOpenAI", Mocked)

    async def allowed():
        return None

    monkeypatch.setattr(
        "django_ergo.conversation.spend_guard.check_unguarded_dispatch", allowed
    )
    from django_ergo.conversation.attachments import openai_transcription

    with override_settings(
        DJANGO_ERGO={"VOICE_OPENAI_API_KEY_ENV": "REVIEW_VOICE_KEY"}
    ):
        with pytest.raises(ValueError, match="REVIEW_VOICE_KEY"):
            voice_openai_key()
        with pytest.raises(ValueError, match="REVIEW_VOICE_KEY"):
            asyncio.run(openai_transcription(b"x", "audio/ogg", "a.ogg"))
        assert seen == []  # nothing reached the transport
        monkeypatch.setenv("REVIEW_VOICE_KEY", "fake-voice-key")
        result = asyncio.run(openai_transcription(b"x", "audio/ogg", "a.ogg"))
    assert result.text == "hello"
    assert seen == ["Bearer fake-voice-key"]


def test_control_concurrent_embedding_providers_never_cross_keys(monkeypatch):
    monkeypatch.setenv("REVIEW_KEY_A", "fake-key-a")
    monkeypatch.setenv("REVIEW_KEY_B", "fake-key-b")
    before = getattr(openai, "api_key", None)
    seen: dict[str, list[str]] = {"a": [], "b": []}

    def provider(label, env):
        found = OpenAIEmbeddingProvider({"api_key_env": env})

        def handler(request):
            seen[label].append(request.headers["authorization"])
            return _embedding_response(request)

        found.client = found.client.with_options(
            http_client=httpx.Client(transport=httpx.MockTransport(handler))
        )
        return found

    a, b = provider("a", "REVIEW_KEY_A"), provider("b", "REVIEW_KEY_B")
    barrier = threading.Barrier(8)

    def work(p):
        barrier.wait()
        for _ in range(10):
            p.generate_embedding("text")

    threads = [threading.Thread(target=work, args=(p,)) for p in [a, b] * 4]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen["a"] == ["Bearer fake-key-a"] * 40
    assert seen["b"] == ["Bearer fake-key-b"] * 40
    assert getattr(openai, "api_key", None) == before  # no module-global mutation


@pytest.mark.django_db(transaction=True)
def test_control_key_env_rename_keeps_one_weekly_cap_and_fails_closed(
    clock,  # noqa: F811
    monkeypatch,
):
    """A rename is a new transport identity: queued/granted routes refuse until
    an admin maps it, it can only join the same vendor family, and spend before
    and after the rename shares one weekly cap (no fresh budget)."""
    from django_ergo.bots import pools
    from django_ergo.bots.providers import parse_providers
    from django_ergo.conversation import spend_guard
    from django_ergo.conversation import weekly_budgets as weekly
    from django_ergo.conversation.models import SpendFunding
    from tests.test_model_pools import POOLS
    from tests.test_weekly_provider_budgets import END
    from tests.test_weekly_provider_budgets import START
    from tests.test_weekly_provider_budgets import reserve
    from tests.test_weekly_provider_budgets import settle
    from tests.test_weekly_provider_budgets import setup_budget

    owner, admin, providers, grant = setup_budget(clock)
    settle(reserve(owner, providers, grant, "80"), "80")
    reserve(owner, providers, grant, "15")
    before_reserved = weekly.inspect_family("openai")["reserved_usd"]
    monkeypatch.setenv("REVIEW_RENAMED_ORG_KEY", "fake-renamed")
    renamed = parse_providers(
        POOLS.replace("budget_usd: 10", "budget_usd: 100").replace(
            "api_key_env: TEST_POOL_OPENAI_KEY", "api_key_env: REVIEW_RENAMED_ORG_KEY"
        )
    )
    new_identity = pools.transport_identity(renamed.providers["openai"], "gpt-6.1-sol")
    assert "REVIEW_RENAMED_ORG_KEY" in new_identity
    # Unmapped, it still resolves to the existing vendor family by endpoint.
    assert weekly.family_of(new_identity) == "openai"
    with pytest.raises(spend_guard.SpendDenied):
        reserve(owner, renamed, grant, "1")  # old grant, new identity: refused
    with pytest.raises(ValueError, match="billed by openai"):
        weekly.assert_mapping(
            actor_id=admin.pk,
            owner_id=owner.pk,
            identity=new_identity,
            account_label="openai",
            assertion="Wrong family attempt",
            family="anthropic",
        )
    weekly.assert_mapping(
        actor_id=admin.pk,
        owner_id=owner.pk,
        identity=new_identity,
        account_label="openai",
        assertion="Same org account under a renamed selector",
    )
    config = renamed.pools.get("paid")
    pools.approve_funding(
        renamed,
        "paid",
        by_id=owner.pk,
        budget_usd="100",
        expires_at=END,
        valid_from=START,
        accounts=pools.funding_accounts(renamed, config),
        budget_period=weekly.WEEKLY,
        version=SpendFunding.objects.order_by("-pk").first().pk,
        note="Re-approve funding for the renamed selector",
    )
    regrant = pools.snapshot(renamed, owner.pk).get("paid").grant
    with pytest.raises(spend_guard.SpendDenied, match="weekly"):
        reserve(owner, renamed, regrant, "30")  # 80 + 30 > the one $100 cap
    assert Decimal(weekly.inspect_family("openai")["spent_usd"]) == Decimal(80)
    assert weekly.inspect_family("openai")["reserved_usd"] == before_reserved
    assert Decimal(before_reserved) == Decimal(15)
