"""The Gemini spend-guard adapter and the google engine under a metered pool
route (bounds from the wire body, strict usageMetadata, one reservation per
HTTP attempt). No test reaches the network: HTTP is httpx.MockTransport."""

from __future__ import annotations

import copy
import datetime as dt
import json
from decimal import Decimal

import httpx
import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from django_ergo.bots import pools
from django_ergo.bots.providers import ProvidersError
from django_ergo.bots.providers import parse_providers
from django_ergo.conversation import spend_guard
from django_ergo.conversation.engines import google_api
from django_ergo.conversation.engines.google_api import GoogleAPIEngine
from django_ergo.conversation.engines.google_errors import InternalServerError
from django_ergo.conversation.engines.google_spend import GoogleAdapter
from django_ergo.conversation.models import InferenceRequest
from django_ergo.pricing import ExactPrice
from django_ergo.pricing import LongContextRates

User = get_user_model()
MODEL = "gemini-2.5-pro"
PRICE = ExactPrice(
    provider="gemini",
    model=MODEL,
    input=Decimal("1.25"),
    cached=Decimal("0.125"),
    output=Decimal(10),
    long_context=LongContextRates(
        threshold=200_000,
        input=Decimal("2.5"),
        cached=Decimal("0.25"),
        output=Decimal(15),
    ),
)
FLASH = ExactPrice(
    provider="gemini",
    model="gemini-2.5-flash",
    input=Decimal("0.30"),
    cached=Decimal("0.03"),
    output=Decimal("2.50"),
)

POOLS = """
default: auto/medium
providers:
  gemini:
    type: google
    api_key_env: TEST_GEMINI_KEY
    models:
      - {name: gemini-2.5-pro, price: {input: 1.25, cached: 0.125, output: 10}}
      - gemini-2.5-flash
  claude:
    type: claude
    transport: cli
    models: [claude-opus-5-5]
tiers:
  medium: [claude/claude-opus-5-5, gemini/gemini-2.5-pro]
agents:
  medium:
    - {agent: claude, model: claude-opus-5-5, provider: claude}
pools:
  - name: subs
    routes: [claude]
  - name: paid
    owner: lee
    routes: [gemini]
    budget_usd: 10
    expires: 2099-12-31
"""


def body(**extra):
    out = {
        "model": MODEL,
        "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
        "generationConfig": {"maxOutputTokens": 500},
    }
    out.update(extra)
    return out


def usage_meta(**extra):
    meta = {
        "promptTokenCount": 100,
        "cachedContentTokenCount": 40,
        "candidatesTokenCount": 10,
        "thoughtsTokenCount": 30,
        "totalTokenCount": 140,
    }
    meta.update(extra)
    return meta


# -- bound: what the wire body costs at most ------------------------------------


def test_bound_clamps_output_to_the_pool_ceiling_in_place():
    kwargs = body()
    bound = GoogleAdapter().bound(kwargs, 200)
    assert kwargs["generationConfig"]["maxOutputTokens"] == 200
    assert bound.output_tokens == 200 and not bound.cache_write
    open_ended = body()
    del open_ended["generationConfig"]
    assert GoogleAdapter().bound(open_ended, 300).output_tokens == 300
    assert open_ended["generationConfig"]["maxOutputTokens"] == 300
    assert GoogleAdapter().bound(body(), 9000).output_tokens == 500  # lower wins


def test_bound_counts_contents_system_instruction_and_tools():
    base = GoogleAdapter().bound(body(), 500).input_tokens
    big = "x" * 10_000
    for key, value in (
        ("systemInstruction", {"parts": [{"text": big}]}),
        ("tools", [{"functionDeclarations": [{"name": "f", "description": big}]}]),
    ):
        got = GoogleAdapter().bound(body(**{key: value}), 500).input_tokens
        assert got >= base + 10_000, key
    more = body()
    more["contents"].append({"role": "model", "parts": [{"text": big}]})
    assert GoogleAdapter().bound(more, 500).input_tokens >= base + 10_000


@pytest.mark.parametrize(
    "extra",
    [
        {"tools": [{"googleSearch": {}}]},
        {"tools": [{"googleSearchRetrieval": {}}]},
        {"tools": [{"codeExecution": {}}]},
        {"tools": [{"urlContext": {}}]},
        {"tools": [{"functionDeclarations": [], "googleMaps": {}}]},
        {"cachedContent": "cachedContents/abc"},  # storage fees
        {"safetySettings": []},
    ],
)
def test_server_side_tools_and_opaque_options_are_refused(extra):
    with pytest.raises(spend_guard.UnsupportedRequest):
        GoogleAdapter().bound(body(**extra), 500)


@pytest.mark.parametrize(
    "part",
    [
        {"inlineData": {"mimeType": "image/png", "data": "AAAA"}},
        {"inlineData": {"mimeType": "application/pdf", "data": "AAAA"}},
        {"inlineData": {"mimeType": "audio/wav", "data": "AAAA"}},
        {"fileData": {"mimeType": "video/mp4", "fileUri": "https://x/v.mp4"}},
        {"videoMetadata": {}},
    ],
)
def test_non_text_input_is_refused(part):
    kwargs = body()
    kwargs["contents"][0]["parts"].append(part)
    with pytest.raises(spend_guard.UnsupportedRequest, match="no exact token bound"):
        GoogleAdapter().bound(kwargs, 500)


@pytest.mark.parametrize(
    "config",
    [
        {"candidateCount": 2},
        {"responseModalities": ["TEXT", "AUDIO"]},
        {"speechConfig": {}},
        {"maxOutputTokens": 0},
        {"maxOutputTokens": "lots"},
    ],
)
def test_unbounded_generation_options_are_refused(config):
    with pytest.raises(spend_guard.UnsupportedRequest):
        GoogleAdapter().bound(body(generationConfig=config), 500)


def test_a_long_prompt_needs_a_long_context_rate():
    huge = body(systemInstruction={"parts": [{"text": "x" * 250_000}]})
    with pytest.raises(spend_guard.UnsupportedRequest, match="long_context"):
        GoogleAdapter(FLASH).bound(copy.deepcopy(huge), 500)
    with pytest.raises(spend_guard.UnsupportedRequest, match="long_context"):
        GoogleAdapter(None).bound(copy.deepcopy(huge), 500)
    bound = GoogleAdapter(PRICE).bound(copy.deepcopy(huge), 500)
    assert bound.input_tokens > 200_000
    # the guard then reserves at the dearest (long-context) tier
    worst = PRICE.worst_case(bound.input_tokens, 500, cache_write=False)
    assert worst == (bound.input_tokens * Decimal("2.5") + 500 * Decimal(15)) / Decimal(
        1_000_000
    )


# -- usage: what a response billed ---------------------------------------------


def test_usage_splits_cached_prompt_and_counts_thoughts_as_output():
    got = GoogleAdapter().usage({"usageMetadata": usage_meta()})
    assert got == spend_guard.Usage(uncached=60, cached=40, cache_write=0, output=40)
    priced = spend_guard.priced(got, FLASH)
    assert priced["cost_status"] == "priced"
    assert priced["cost_usd"] == (
        60 * Decimal("0.30") + 40 * Decimal("0.03") + 40 * Decimal("2.50")
    ) / Decimal(1_000_000)


def test_tool_use_prompt_tokens_are_billed_as_input():
    got = GoogleAdapter().usage(
        {"usageMetadata": usage_meta(toolUsePromptTokenCount=7, totalTokenCount=147)}
    )
    assert (got.uncached, got.cached, got.output) == (67, 40, 40)


def test_thoughts_may_be_absent_only_when_the_total_proves_it():
    ok = {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15}
    assert GoogleAdapter().usage({"usageMetadata": ok}).output == 5
    hidden = {**ok, "totalTokenCount": 45}  # thoughts exist but aren't reported
    assert GoogleAdapter().usage({"usageMetadata": hidden}) is None
    no_total = {"promptTokenCount": 10, "candidatesTokenCount": 5}
    assert GoogleAdapter().usage({"usageMetadata": no_total}) is None


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"usageMetadata": None},
        {"usageMetadata": {}},
        {"usageMetadata": {"promptTokenCount": 10}},  # no candidates count
        {"usageMetadata": {"candidatesTokenCount": 10}},
        {"usageMetadata": usage_meta(promptTokenCount="100")},
        {"usageMetadata": usage_meta(promptTokenCount=-1)},
        {"usageMetadata": usage_meta(candidatesTokenCount=True)},
        {"usageMetadata": usage_meta(thoughtsTokenCount=-5)},
        {"usageMetadata": usage_meta(cachedContentTokenCount=101)},
        {"usageMetadata": usage_meta(totalTokenCount=999)},
        {
            "usageMetadata": usage_meta(
                promptTokensDetails=[{"modality": "AUDIO", "tokenCount": 5}]
            )
        },
        {
            "usageMetadata": usage_meta(),
            "candidates": [{"groundingMetadata": {"webSearchQueries": ["x"]}}],
        },
    ],
)
def test_missing_or_malformed_usage_is_none_so_the_hold_stays(response):
    assert GoogleAdapter().usage(response) is None


# -- the engine under a guard ---------------------------------------------------


def reply(text="ok", usage=None):
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": text}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": usage if usage is not None else usage_meta(),
    }


class Wire:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item)

    @property
    def body(self):
        return json.loads(self.requests[-1].content)


@pytest.fixture
def wire(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-123")

    def install(*responses):
        w = Wire(*responses)
        monkeypatch.setattr(google_api, "_transport", lambda: httpx.MockTransport(w))
        return w

    return install


@pytest.fixture
def lee(db):
    user, _ = User.objects.get_or_create(username="lee")
    return user


def held(lee, route=f"gemini/{MODEL}", budget="10"):
    providers = parse_providers(POOLS)
    return pools.issue_grant(
        providers,
        "paid",
        issuer_id=lee.pk,
        grantee_id=lee.pk,
        budget_usd=budget,
        expires_at=timezone.now() + dt.timedelta(days=7),
        routes=[route],
    )


def guarded(lee, grant, price=PRICE, model=MODEL, **config):
    engine = GoogleAPIEngine({"model": model, "max_tokens": 5000, **config})
    engine.spend_guard = spend_guard.MeteredGuard(
        pool="paid",
        provider="gemini",
        model=model,
        grant_id=grant.pk,
        user_id=lee.pk,
        price=price,
        max_output_tokens=1000,
        current=lambda grant: spend_guard.Terms.of(grant),
    )
    return engine


MESSAGES = [{"role": "user", "content": "hi"}]


@pytest.mark.django_db(transaction=True)
async def test_revocation_stops_an_already_built_google_engine(wire, lee):
    from asgiref.sync import sync_to_async

    grant = await _agrant(lee)
    engine = guarded(lee, grant)
    engine.spend_guard.current = pools.SendAuthority(parse_providers(POOLS))
    w = wire(reply())
    await engine.complete(MESSAGES)
    await sync_to_async(pools.revoke_grant)(
        parse_providers(POOLS), grant.pk, by_id=lee.pk
    )
    with pytest.raises(spend_guard.SpendDenied, match="revoked"):
        await engine.complete(MESSAGES)
    assert len(w.requests) == 1


@pytest.mark.django_db(transaction=True)
async def test_long_context_uses_current_price_for_an_already_built_engine(wire, lee):
    grant = await _agrant(lee)
    engine = guarded(lee, grant)  # built with PRICE, which has long-context rates
    engine.spend_guard.current = pools.SendAuthority(parse_providers(POOLS))
    w = wire(reply())  # current config has no long-context rates
    with pytest.raises(spend_guard.SpendDenied, match="long_context"):
        await engine.complete([{"role": "user", "content": "x" * 210_000}])
    assert not w.requests


@pytest.mark.django_db(transaction=True)
async def test_a_guarded_send_clamps_reserves_and_prices_exactly(wire, lee):
    grant = await _agrant(lee)
    w = wire(reply())
    engine = guarded(lee, grant)
    await engine.complete(MESSAGES)
    assert w.body["generationConfig"]["maxOutputTokens"] == 1000  # pool ceiling
    row = await InferenceRequest.objects.aget(billing_mode="metered")
    assert row.pool == f"grant:{grant.pk}"
    assert row.cost_status == "priced"
    assert row.cost_usd == (
        60 * Decimal("1.25") + 40 * Decimal("0.125") + 40 * Decimal(10)
    ) / Decimal(1_000_000)
    assert (row.prompt_tokens, row.cached_tokens, row.output_tokens) == (100, 40, 40)
    assert row.reserved_usd > row.cost_usd  # worst case held first


async def _agrant(lee):
    from asgiref.sync import sync_to_async

    return await sync_to_async(held)(lee)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "usage", [{}, {"promptTokenCount": 5}, usage_meta(candidatesTokenCount=-1)]
)
async def test_unusable_usage_keeps_the_reservation(wire, lee, usage):
    grant = await _agrant(lee)
    wire(reply(usage=usage))
    await guarded(lee, grant).complete(MESSAGES)
    row = await InferenceRequest.objects.aget(billing_mode="metered")
    assert row.cost_status == "unknown" and row.cost_usd is None
    assert row.reserved_usd > 0


@pytest.mark.django_db(transaction=True)
async def test_a_denied_request_sends_nothing(wire, lee):
    grant = await _agrant(lee)
    w = wire(reply())
    engine = guarded(lee, grant)
    tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    with pytest.raises(spend_guard.SpendDenied, match="can't bound"):
        await engine.complete(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,AAAA"},
                        }
                    ],
                }
            ],
            tools=tools,
        )
    assert w.requests == []
    assert not await InferenceRequest.objects.filter(billing_mode="metered").aexists()


@pytest.mark.django_db(transaction=True)
async def test_no_exact_price_or_no_long_context_rate_sends_nothing(wire, lee):
    grant = await _agrant(lee)
    w = wire(reply())
    with pytest.raises(spend_guard.SpendDenied, match="no exact price"):
        await guarded(lee, grant, price=None).complete(MESSAGES)
    with pytest.raises(spend_guard.SpendDenied, match="long_context"):
        await guarded(lee, grant, price=FLASH, model=MODEL).complete(
            [{"role": "user", "content": "x" * 250_000}]
        )
    assert w.requests == []


@pytest.mark.django_db(transaction=True)
async def test_every_attempt_has_its_own_reservation_and_nothing_retries(wire, lee):
    grant = await _agrant(lee)
    w = wire(
        httpx.Response(500, json={"error": {"code": 500, "message": "boom"}}),
        reply(),
    )
    engine = guarded(lee, grant)
    with pytest.raises(InternalServerError):
        await engine.complete(MESSAGES)
    assert len(w.requests) == 1  # no hidden retry inside the send path
    rows = [r async for r in InferenceRequest.objects.filter(billing_mode="metered")]
    assert [r.cost_status for r in rows] == ["unknown"]  # may have reached Google
    held_after_failure = rows[0].reserved_usd
    await engine.complete(MESSAGES)  # the permitted retry
    assert len(w.requests) == 2
    rows = [
        r
        async for r in InferenceRequest.objects.filter(billing_mode="metered").order_by(
            "attempt"
        )
    ]
    assert [r.attempt for r in rows] == [1, 2]
    assert [r.cost_status for r in rows] == ["unknown", "priced"]
    assert held_after_failure > 0


@pytest.mark.django_db(transaction=True)
async def test_a_retry_that_would_pass_the_budget_is_refused_unsent(wire, lee):
    from asgiref.sync import sync_to_async

    grant = await sync_to_async(held)(lee, budget="0.02")
    w = wire(httpx.Response(500, json={"error": {"code": 500}}), reply())
    engine = guarded(lee, grant)
    with pytest.raises(InternalServerError):
        await engine.complete(MESSAGES)
    with pytest.raises(spend_guard.SpendDenied, match="budget"):
        await engine.complete(MESSAGES)  # the failed send's hold still counts
    assert len(w.requests) == 1


@pytest.mark.django_db(transaction=True)
async def test_streaming_goes_through_the_guard_once(wire, lee):
    grant = await _agrant(lee)
    chunk = json.dumps(reply())
    w = wire(
        httpx.Response(
            200,
            content=f"data: {chunk}\n\n".encode(),
            headers={"content-type": "text/event-stream"},
        )
    )
    await guarded(lee, grant, stream=True).complete(MESSAGES)
    assert len(w.requests) == 1
    row = await InferenceRequest.objects.aget(billing_mode="metered")
    assert row.cost_status == "priced"


def test_a_guard_never_prices_from_the_telemetry_table():
    # the table has rows for gemini models; the adapter's long-context check and
    # the guard's reservation use only the route's exact ExactPrice.
    from django_ergo.pricing import price_for

    assert price_for("gemini-2.5-flash") is not None
    with pytest.raises(spend_guard.UnsupportedRequest):
        GoogleAdapter(None).bound(
            body(systemInstruction={"parts": [{"text": "x" * 250_000}]}), 500
        )


# -- resolver: when a Gemini route is listed -----------------------------------


def _snap(text=POOLS):
    providers = parse_providers(text)
    return pools.Snapshot(providers=providers, pools=[], auto_rollover=False)


def test_a_metered_gemini_route_needs_the_guard_path_and_an_exact_price(monkeypatch):
    monkeypatch.setenv("TEST_GEMINI_KEY", "k")
    snap = _snap()
    privacy = pools.Privacy()
    assert spend_guard_adapter_present()
    assert pools.route_problem(snap, privacy, pools.CHAT, "gemini", MODEL) is None
    unpriced = pools.route_problem(
        snap, privacy, pools.CHAT, "gemini", "gemini-2.5-flash"
    )
    assert unpriced[0] == pools.PRICE_UNKNOWN  # no table fallback
    # without the adapter the engine can't carry a metered route
    monkeypatch.setattr(GoogleAPIEngine, "spend_adapter", None)
    problem = pools.route_problem(snap, privacy, pools.CHAT, "gemini", MODEL)
    assert problem[0] == pools.UNGUARDED


def spend_guard_adapter_present() -> bool:
    return isinstance(GoogleAPIEngine.spend_adapter, GoogleAdapter)


def test_gemini_is_never_a_coding_route(monkeypatch):
    monkeypatch.setenv("TEST_GEMINI_KEY", "k")
    providers = parse_providers(POOLS)
    paid = next(p for p in providers.pools.pools if p.name == "paid")
    # nothing derives a coding agent from a google provider
    assert pools.agent_candidates(providers, paid, "medium") == []
    # and a hand-written agents: entry can't name one (subscriptions only)
    bad = POOLS.replace(
        "    - {agent: claude, model: claude-opus-5-5, provider: claude}",
        "    - {agent: codex, model: gemini-2.5-pro, provider: gemini}",
    )
    with pytest.raises(ProvidersError, match="subscription"):
        parse_providers(bad)
