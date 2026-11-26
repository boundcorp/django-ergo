"""Tests for the Google Gemini engine. HTTP is mocked; nothing here reaches the
network. The one live smoke test is skipped unless ERGO_LIVE_GEMINI=1."""

from __future__ import annotations

import json
import os
from decimal import Decimal

import httpx
import pytest
from django.contrib.auth import get_user_model
from pydantic import BaseModel

from django_ergo.bots.providers import ProvidersError
from django_ergo.bots.providers import parse_providers
from django_ergo.conversation import structured
from django_ergo.conversation.budget import BudgetExceeded
from django_ergo.conversation.budget import RequestLimits
from django_ergo.conversation.engines import ENGINE_REGISTRY
from django_ergo.conversation.engines import google_api
from django_ergo.conversation.engines import google_wire as wire
from django_ergo.conversation.engines.google_api import GoogleAPIEngine
from django_ergo.conversation.engines.google_errors import APIConnectionError
from django_ergo.conversation.engines.google_errors import APITimeoutError
from django_ergo.conversation.engines.google_errors import AuthenticationError
from django_ergo.conversation.engines.google_errors import BadRequestError
from django_ergo.conversation.engines.google_errors import EmptyResponseError
from django_ergo.conversation.engines.google_errors import InternalServerError
from django_ergo.conversation.engines.google_errors import OverloadedError
from django_ergo.conversation.engines.google_errors import QuotaExhaustedError
from django_ergo.conversation.engines.google_errors import RateLimitError
from django_ergo.conversation.engines.openai_api import OpenAIAPIEngine
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import EngineType
from django_ergo.conversation.models import MessageBlock
from django_ergo.conversation.models import SessionStatus
from django_ergo.pricing import price_for

User = get_user_model()
KEY = "test-gemini-key-123"
TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Weather for a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City"}},
            "required": ["city"],
        },
    },
}


def candidate(parts, finish="STOP", usage=None):
    return {
        "candidates": [
            {"content": {"role": "model", "parts": parts}, "finishReason": finish}
        ],
        "usageMetadata": usage
        or {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15},
        "modelVersion": "gemini-2.5-flash",
    }


class Recorder:
    """A mock Gemini: records requests, answers from a queue."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item)

    @property
    def body(self) -> dict:
        return json.loads(self.requests[-1].content)


@pytest.fixture
def mock(monkeypatch):
    def install(*responses):
        recorder = Recorder(*responses)
        monkeypatch.setattr(
            google_api, "_transport", lambda: httpx.MockTransport(recorder)
        )
        return recorder

    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    return install


@pytest.fixture
def engine():
    return GoogleAPIEngine({"model": "gemini-2.5-flash", "max_tokens": 1000})


# -- request mapping ------------------------------------------------------------


def test_registered_as_google_api():
    assert ENGINE_REGISTRY[("google", "api")].endswith("GoogleAPIEngine")
    assert GoogleAPIEngine.engine_type == "google"


def test_request_maps_system_history_and_tools():
    messages = [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "weather in Paris?"},
    ]
    body = wire.build_request(
        messages,
        model="gemini-2.5-flash",
        tools=[TOOL],
        max_tokens=500,
        temperature=0.2,
    )
    assert body["systemInstruction"] == {"parts": [{"text": "Be brief."}]}
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    assert body["contents"][1]["parts"] == [{"text": "hello"}]
    assert body["generationConfig"]["maxOutputTokens"] == 500
    assert body["generationConfig"]["temperature"] == 0.2
    declaration = body["tools"][0]["functionDeclarations"][0]
    assert declaration["name"] == "get_weather"
    assert declaration["parameters"]["properties"]["city"]["type"] == "string"
    assert declaration["parameters"]["required"] == ["city"]
    assert body["toolConfig"]["functionCallingConfig"] == {"mode": "AUTO"}


def test_image_audio_and_document_parts():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA"},
                },
                {"type": "image_url", "image_url": {"url": "https://x.test/a.png"}},
                {
                    "type": "file",
                    "file": {
                        "filename": "a.pdf",
                        "file_data": "data:application/pdf;base64,BBBB",
                    },
                },
                {
                    "type": "input_audio",
                    "input_audio": {"data": "CCCC", "format": "mp3"},
                },
            ],
        }
    ]
    _, contents = wire.to_contents(messages)
    parts = contents[0]["parts"]
    assert parts[0] == {"text": "look"}
    assert parts[1] == {"inlineData": {"mimeType": "image/png", "data": "AAAA"}}
    assert parts[2] == {"text": "[image: https://x.test/a.png]"}
    assert parts[3] == {"inlineData": {"mimeType": "application/pdf", "data": "BBBB"}}
    assert parts[4] == {"inlineData": {"mimeType": "audio/mp3", "data": "CCCC"}}


def test_parallel_calls_and_results_round_trip():
    messages = [
        {"role": "user", "content": "both"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_a",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"city": "Paris"}',
                    },
                    "thought_signature": "SIG",
                },
                {
                    "id": "g-2",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"city": "Rome"}',
                    },
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_a", "content": "sunny"},
        {"role": "tool", "tool_call_id": "g-2", "content": "boom", "is_error": True},
    ]
    _, contents = wire.to_contents(messages, model="gemini-2.5-flash")
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    calls = contents[1]["parts"]
    assert calls[0]["functionCall"] == {
        "name": "get_weather",
        "args": {"city": "Paris"},
    }
    assert calls[0]["thoughtSignature"] == "SIG"
    # an id Google issued goes back; one we made up doesn't
    assert calls[1]["functionCall"]["id"] == "g-2"
    results = contents[2]["parts"]
    assert results[0]["functionResponse"] == {
        "name": "get_weather",
        "response": {"result": "sunny"},
    }
    assert results[1]["functionResponse"]["response"] == {"error": "boom"}
    assert results[1]["functionResponse"]["id"] == "g-2"


def test_gemini_3_gets_documented_dummy_signature_when_none_stored():
    messages = [
        {"role": "user", "content": "x"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "t", "arguments": "{}"},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "t", "arguments": "{}"},
                },
            ],
        },
    ]
    _, contents = wire.to_contents(messages, model="gemini-3-pro-preview")
    first, second = contents[1]["parts"]
    assert first["thoughtSignature"] == wire.DUMMY_SIGNATURE
    assert "thoughtSignature" not in second
    _, contents = wire.to_contents(messages, model="gemini-2.5-pro")
    assert "thoughtSignature" not in contents[1]["parts"][0]


def test_schema_is_reduced_to_the_gemini_subset():
    schema = {
        "$defs": {"Pt": {"type": "object", "properties": {"x": {"type": "integer"}}}},
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "pt": {"$ref": "#/$defs/Pt"},
            "note": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
            "kind": {"const": "a"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["pt", "gone"],
    }
    out = wire.clean_schema(schema)
    assert "additionalProperties" not in out
    assert out["properties"]["pt"]["properties"]["x"] == {"type": "integer"}
    assert out["properties"]["note"] == {"type": "string", "nullable": True}
    assert out["properties"]["kind"]["enum"] == ["a"]
    assert out["required"] == ["pt"]


@pytest.mark.parametrize(
    ("model", "effort", "expected"),
    [
        ("gemini-2.5-pro", "none", {"thinkingBudget": 128}),  # can't be disabled
        ("gemini-2.5-flash", "none", {"thinkingBudget": 0}),
        ("gemini-2.5-flash", "medium", {"thinkingBudget": 8192}),
        ("gemini-2.5-flash", "xhigh", {"thinkingBudget": 24576}),  # clamped
        ("gemini-3-pro-preview", "medium", {"thinkingLevel": "low"}),
        ("gemini-3-pro-preview", "high", {"thinkingLevel": "high"}),
        ("gemini-3-flash-preview", "minimal", {"thinkingLevel": "minimal"}),
        ("gemini-3.1-pro-preview", "medium", {"thinkingLevel": "medium"}),
        ("gemini-2.0-flash", "high", None),
        ("gemini-2.5-flash", None, None),
    ],
)
def test_effort_maps_to_thinking_config(model, effort, expected):
    assert wire.thinking_config(model, effort) == expected


def test_explicit_thinking_settings_and_thoughts():
    assert wire.thinking_config(
        "gemini-2.5-flash", budget=-1, include_thoughts=True
    ) == {
        "thinkingBudget": -1,
        "includeThoughts": True,
    }
    assert wire.thinking_config("gemini-3-flash-preview", level="high") == {
        "thinkingLevel": "high"
    }


# -- response parsing and usage ---------------------------------------------------


def test_parallel_function_calls_parsed():
    data = candidate(
        [
            {"functionCall": {"name": "a", "args": {"x": 1}}, "thoughtSignature": "S"},
            {"functionCall": {"name": "b", "args": {}, "id": "g-9"}},
        ]
    )
    reply = wire.parse_response(data)
    assert [c["name"] for c in reply.calls] == ["a", "b"]
    assert reply.calls[0]["id"].startswith(wire.CALL_ID_PREFIX)
    assert reply.calls[0]["thought_signature"] == "S"
    assert reply.calls[1]["id"] == "g-9"
    assert reply.finish_reason == "tool_calls"


def test_usage_splits_cached_and_thinking_tokens():
    usage = wire.usage_parts(
        {
            "promptTokenCount": 1000,
            "cachedContentTokenCount": 400,
            "candidatesTokenCount": 50,
            "thoughtsTokenCount": 200,
        }
    )
    assert usage == {
        "input_tokens": 600,
        "cache_read_input_tokens": 400,
        "cache_creation_input_tokens": 0,
        "output_tokens": 250,
        "reasoning_tokens": 200,
    }
    assert wire.usage_parts(None)["input_tokens"] is None


def test_finish_reasons_map():
    assert (
        wire.parse_response(candidate([{"text": "x"}], "MAX_TOKENS")).finish_reason
        == "length"
    )
    blocked = wire.parse_response(candidate([], "SAFETY"))
    assert (blocked.finish_reason, blocked.refusal_category) == (
        "content_filter",
        "SAFETY",
    )
    prompt_block = wire.parse_response(
        {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}, "usageMetadata": {}}
    )
    assert prompt_block.finish_reason == "content_filter"
    with pytest.raises(InternalServerError):
        wire.parse_response(candidate([], "MALFORMED_FUNCTION_CALL"))
    with pytest.raises(EmptyResponseError):
        wire.parse_response({"candidates": []})


def test_stream_chunks_merge():
    chunks = [
        candidate([{"text": "Hel"}], finish=None, usage={"promptTokenCount": 3}),
        candidate([{"text": "lo"}], finish=None),
        candidate(
            [{"functionCall": {"name": "a", "args": {}}}],
            usage={"promptTokenCount": 3, "candidatesTokenCount": 7},
        ),
    ]
    for chunk in chunks[:2]:
        chunk["candidates"][0].pop("finishReason")
    reply = wire.parse_response(wire.merge_stream(chunks))
    assert reply.text == "Hello"
    assert [c["name"] for c in reply.calls] == ["a"]
    assert reply.usage["output_tokens"] == 7


# -- error mapping -------------------------------------------------------------------


def error(status, reason, message, details=None):
    return {
        "error": {
            "code": status,
            "status": reason,
            "message": message,
            "details": details or [],
        }
    }


@pytest.mark.parametrize(
    ("status", "body", "cls"),
    [
        (401, error(401, "UNAUTHENTICATED", "bad"), AuthenticationError),
        (403, error(403, "PERMISSION_DENIED", "no"), AuthenticationError),
        (
            400,
            error(
                400,
                "INVALID_ARGUMENT",
                "API key not valid. Please pass a valid API key.",
            ),
            AuthenticationError,
        ),
        (400, error(400, "INVALID_ARGUMENT", "bad field"), BadRequestError),
        (404, error(404, "NOT_FOUND", "model gone"), BadRequestError),
        (429, error(429, "RESOURCE_EXHAUSTED", "Too many requests"), RateLimitError),
        (503, error(503, "UNAVAILABLE", "overloaded"), OverloadedError),
        (500, error(500, "INTERNAL", "oops"), InternalServerError),
    ],
)
def test_http_errors_are_typed(status, body, cls):
    exc = wire.error_for(status, body, key=KEY)
    assert type(exc) is cls
    assert exc.status == status


def test_429_distinguishes_exhausted_from_rate():
    daily = wire.error_for(
        429,
        error(
            429,
            "RESOURCE_EXHAUSTED",
            "You exceeded your current quota",
            [
                {
                    "violations": [
                        {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}
                    ]
                }
            ],
        ),
    )
    assert isinstance(daily, QuotaExhaustedError)
    assert daily.exhausted is True
    rate = wire.error_for(
        429,
        error(
            429,
            "RESOURCE_EXHAUSTED",
            "slow down",
            [
                {
                    "retryDelay": "21s",
                    "violations": [{"quotaId": "GenerateRequestsPerMinute"}],
                }
            ],
        ),
    )
    assert type(rate) is RateLimitError
    assert rate.exhausted is False
    assert rate.retry_after == 21.0
    # routing's string check sees both as limit errors
    from django_ergo.bots.routing import is_limit_error

    assert is_limit_error(str(daily)) and is_limit_error(str(rate))


def test_error_message_never_contains_the_key():
    exc = wire.error_for(400, error(400, "INVALID_ARGUMENT", f"bad key {KEY}"), key=KEY)
    assert KEY not in str(exc)


def test_structured_call_classifies_errors_by_name():
    assert structured._error_category(AuthenticationError("x")) == "auth"
    assert structured._error_category(RateLimitError("x")) == "network"
    assert structured._error_category(QuotaExhaustedError("x")) == "network"
    assert structured._error_category(BadRequestError("x")) == "model"
    assert structured._is_transient(OverloadedError("x"))
    assert structured._is_transient(InternalServerError("x"))
    assert structured._is_transient(APITimeoutError("x"))
    assert structured._is_transient(APIConnectionError("x"))
    assert not structured._is_transient(AuthenticationError("x"))
    assert not structured._is_transient(RateLimitError("x"))


# -- the engine, over a mocked transport ----------------------------------------------


async def test_complete_sends_key_header_and_fixed_url(mock, engine):
    rec = mock(candidate([{"text": "hi there"}]))
    result = await engine.complete(
        [{"role": "user", "content": "hi"}], system="Be brief."
    )
    request = rec.requests[0]
    assert request.method == "POST"
    assert str(request.url) == (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.5-flash:generateContent"
    )
    assert request.headers["x-goog-api-key"] == KEY
    assert KEY not in str(request.url)
    assert rec.body["systemInstruction"]["parts"][0]["text"] == "Be brief."
    assert result.message == {"role": "assistant", "content": "hi there"}
    assert (result.input_tokens, result.output_tokens) == (10, 5)
    assert result.model == "gemini-2.5-flash"
    assert KEY not in repr(engine)


async def test_tool_call_round_trip(mock, engine):
    rec = mock(
        candidate(
            [{"functionCall": {"name": "get_weather", "args": {"city": "Paris"}}}]
        ),
        candidate([{"text": "Sunny in Paris."}]),
    )
    messages = [{"role": "user", "content": "weather?"}]
    first = await engine.complete(messages, tools=[TOOL])
    use = next(e.tool_use for e in first.events if e.event_type == "tool_use")
    assert use["name"] == "get_weather"
    assert use["input"] == {"city": "Paris"}
    messages += [
        first.message,
        *engine.tool_result_messages([(use["id"], "sunny", False)]),
    ]
    second = await engine.complete(messages, tools=[TOOL])
    assert second.message["content"] == "Sunny in Paris."
    contents = rec.body["contents"]
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    assert contents[1]["parts"][0]["functionCall"]["name"] == "get_weather"
    assert contents[2]["parts"][0]["functionResponse"] == {
        "name": "get_weather",
        "response": {"result": "sunny"},
    }


async def test_streaming_merges_sse_chunks(mock):
    chunks = [
        candidate([{"text": "Hel"}]),
        candidate(
            [{"text": "lo"}], usage={"promptTokenCount": 4, "candidatesTokenCount": 2}
        ),
    ]
    chunks[0]["candidates"][0].pop("finishReason")
    sse = "".join(f"data: {json.dumps(c)}\r\n\r\n" for c in chunks)
    rec = mock(
        httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})
    )
    engine = GoogleAPIEngine({"model": "gemini-2.5-flash", "stream": True})
    result = await engine.complete([{"role": "user", "content": "hi"}])
    assert str(rec.requests[0].url).endswith(":streamGenerateContent?alt=sse")
    assert result.message["content"] == "Hello"
    assert (result.input_tokens, result.output_tokens) == (4, 2)


async def test_stream_error_chunk_is_typed(mock):
    sse = "data: " + json.dumps(error(429, "RESOURCE_EXHAUSTED", "slow")) + "\n\n"
    mock(httpx.Response(200, content=sse))
    engine = GoogleAPIEngine({"model": "gemini-2.5-flash", "stream": True})
    with pytest.raises(RateLimitError):
        await engine.complete([{"role": "user", "content": "hi"}])


async def test_structured_output_uses_response_schema(mock, engine):
    class Answer(BaseModel):
        city: str
        temp: int

    rec = mock(candidate([{"text": '{"city": "Paris", "temp": 21}'}]))
    out = await engine.generate("weather", response_model=Answer, system="JSON only")
    assert out.raw["parsed"] == Answer(city="Paris", temp=21)
    config = rec.body["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseSchema"]["properties"]["temp"]["type"] == "integer"
    assert "title" not in config["responseSchema"]
    assert out.raw["usage"] == {"prompt_tokens": 10, "completion_tokens": 5}


async def test_effort_reaches_the_request(mock):
    rec = mock(candidate([{"text": "x"}]))
    engine = GoogleAPIEngine(
        {"model": "gemini-3-pro-preview", "reasoning_effort": "high"}
    )
    await engine.complete([{"role": "user", "content": "hi"}])
    assert rec.body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "high"}


async def test_blocked_reply_is_a_content_filter_stop(mock, engine):
    mock(candidate([], "SAFETY"))
    result = await engine.complete([{"role": "user", "content": "hi"}])
    done = result.events[-1]
    assert done.raw["finish_reason"] == "content_filter"
    assert done.raw["refusal_category"] == "SAFETY"


@pytest.mark.parametrize(
    ("response", "cls"),
    [
        (
            httpx.Response(401, json=error(401, "UNAUTHENTICATED", "no")),
            AuthenticationError,
        ),
        (
            httpx.Response(429, json=error(429, "RESOURCE_EXHAUSTED", "slow")),
            RateLimitError,
        ),
        (httpx.Response(503, json=error(503, "UNAVAILABLE", "busy")), OverloadedError),
        (httpx.Response(502, text="<html>bad gateway</html>"), InternalServerError),
        (httpx.ReadTimeout("slow"), APITimeoutError),
        (httpx.ConnectError("dns"), APIConnectionError),
    ],
)
async def test_failures_raise_typed_errors(mock, engine, response, cls):
    mock(response)
    with pytest.raises(cls) as caught:
        await engine.complete([{"role": "user", "content": "hi"}])
    assert KEY not in str(caught.value)


async def test_missing_key_is_an_auth_error_and_sends_nothing(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    rec = Recorder()
    monkeypatch.setattr(google_api, "_transport", lambda: httpx.MockTransport(rec))
    with pytest.raises(AuthenticationError, match="GEMINI_API_KEY"):
        await GoogleAPIEngine({"model": "gemini-2.5-flash"}).complete(
            [{"role": "user", "content": "hi"}]
        )
    assert rec.requests == []
    assert GoogleAPIEngine.configured() is False


async def test_google_api_key_is_the_fallback(monkeypatch, mock):
    mock(candidate([{"text": "ok"}]))
    monkeypatch.delenv("GEMINI_API_KEY")
    monkeypatch.setenv("GOOGLE_API_KEY", "fallback-key")
    assert GoogleAPIEngine.configured() is True
    rec = Recorder(candidate([{"text": "ok"}]))
    monkeypatch.setattr(google_api, "_transport", lambda: httpx.MockTransport(rec))
    await GoogleAPIEngine({"model": "gemini-2.5-flash"}).complete(
        [{"role": "user", "content": "hi"}]
    )
    assert rec.requests[0].headers["x-goog-api-key"] == "fallback-key"


async def test_request_limits_clamp_output_and_refuse_oversize(mock, engine):
    rec = mock(candidate([{"text": "ok"}]))
    engine.request_limits = RequestLimits(
        max_input_tokens=100_000, max_output_tokens=64, timeout_seconds=30
    )
    await engine.complete([{"role": "user", "content": "hi"}])
    assert rec.body["generationConfig"]["maxOutputTokens"] == 64
    engine.request_limits = RequestLimits(
        max_input_tokens=10, max_output_tokens=64, timeout_seconds=30
    )
    with pytest.raises(BudgetExceeded):
        await engine.complete([{"role": "user", "content": "hi"}])
    assert len(rec.requests) == 1


async def test_private_endpoint_is_refused(monkeypatch):
    from django_ergo.conversation.inference_policy import PolicyError

    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    engine = GoogleAPIEngine(
        {"model": "gemini-2.5-flash", "base_url": "https://api.fireworks.ai/x"}
    )
    with pytest.raises(PolicyError):
        await engine.complete([{"role": "user", "content": "hi"}])


# -- a stored session ------------------------------------------------------------------


@pytest.fixture
def session(transactional_db):
    user = User.objects.create_user(username="gemini_user", password="x")
    return ConversationSession.objects.create(
        user=user,
        engine_type="google",
        transport_type="api",
        status=SessionStatus.ACTIVE,
        system_prompt="You are terse.",
    )


@pytest.mark.django_db(transaction=True)
async def test_session_turn_persists_reply_and_usage(mock, engine, session):
    rec = mock(
        candidate(
            [
                {"text": "Checking."},
                {"functionCall": {"name": "get_weather", "args": {"city": "Oslo"}}},
            ],
            usage={
                "promptTokenCount": 100,
                "cachedContentTokenCount": 40,
                "candidatesTokenCount": 10,
                "thoughtsTokenCount": 30,
            },
        ),
        candidate([{"text": "Cold."}]),
    )
    events = [e async for e in engine.send(session, "weather in Oslo?", [TOOL])]
    assert [e.event_type for e in events] == ["text", "tool_use", "done"]
    assert events[-1].raw["finish_reason"] == "tool_calls"
    assert rec.body["systemInstruction"]["parts"][0]["text"] == "You are terse."
    row = await session.messages.filter(role="assistant").afirst()
    assert (
        row.input_tokens,
        row.cache_read_input_tokens,
        row.output_tokens,
        row.reasoning_tokens,
    ) == (60, 40, 40, 30)
    assert row.model_name == "gemini-2.5-flash"
    call = events[1].tool_use
    follow = [
        e
        async for e in engine.submit_tool_results_batch(
            session, [(call["id"], "cold", False)], [TOOL]
        )
    ]
    assert follow[0].text == "Cold."
    contents = rec.body["contents"]
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    assert contents[2]["parts"][0]["functionResponse"]["response"] == {"result": "cold"}


# -- providers.yaml ----------------------------------------------------------------------

PROVIDERS = """
providers:
  google:
    type: google
    key_env: GEMINI_API_KEY
    models:
      - name: gemini-2.5-flash
        price: {input: 0.30, cached: 0.03, output: 2.50}
      - gemini-2.5-pro
"""


def test_google_provider_parses_with_prices(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    providers = parse_providers(PROVIDERS)
    provider = providers.providers["google"]
    assert provider.api_key_env == "GEMINI_API_KEY"
    assert provider.available is False
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    assert provider.available is False
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert provider.available is True
    kind, config, key_env = providers.engine("google/gemini-2.5-flash")
    assert (kind, config["model"], key_env) == (
        "google",
        "gemini-2.5-flash",
        "GEMINI_API_KEY",
    )
    price = providers.find("google/gemini-2.5-flash")[1].price
    assert (price.input, price.cached, price.output) == (
        Decimal("0.30"),
        Decimal("0.03"),
        Decimal("2.50"),
    )
    # an unlisted price stays unlisted: the telemetry table is no fallback
    assert providers.find("google/gemini-2.5-pro")[1].price is None


def test_google_provider_defaults_key_env_and_rejects_cli():
    assert (
        parse_providers("providers:\n  g: {type: google, models: [m]}")
        .providers["g"]
        .api_key_env
        == "GEMINI_API_KEY"
    )
    with pytest.raises(ProvidersError, match="transport"):
        parse_providers("providers:\n  g: {type: google, transport: cli}")


@pytest.mark.parametrize(
    "price",
    ["{input: 1}", "{input: 1, output: -2}", "{input: 1, output: 2, bogus: 3}", "[1]"],
)
def test_bad_prices_are_rejected(price):
    text = f"providers:\n  g:\n    type: google\n    models:\n      - {{name: m, price: {price}}}\n"
    with pytest.raises(ProvidersError, match="price"):
        parse_providers(text)


def test_gemini_costs_are_priced_with_thinking_as_output():
    price = price_for("gemini-2.5-flash-lite")
    assert price.input == 0.10
    parts = price_for("gemini-2.5-pro").parts(input_tokens=250_000, output_tokens=1000)
    assert parts["input"] == pytest.approx(250_000 * 1.25 * 2 / 1e6)  # >200k surcharge


# -- thoughtSignature persistence -----------------------------------------------------


@pytest.fixture
def gemini3():
    return GoogleAPIEngine({"model": "gemini-3.1-pro-preview", "max_tokens": 1000})


def _model_parts(rec, index=1):
    return rec.body["contents"][index]["parts"]


@pytest.mark.django_db(transaction=True)
async def test_signatures_round_trip_through_a_tool_loop(mock, gemini3, session):
    rec = mock(
        candidate(
            [
                {
                    "functionCall": {"name": "get_weather", "args": {"city": "Oslo"}},
                    "thoughtSignature": "SIG-A",
                },
                {"functionCall": {"name": "get_weather", "args": {"city": "Rome"}}},
            ]
        ),
        candidate(
            [
                {
                    "functionCall": {"name": "get_weather", "args": {"city": "Bern"}},
                    "thoughtSignature": "SIG-B",
                }
            ]
        ),
        candidate([{"text": "Done."}]),
    )
    events = [e async for e in gemini3.send(session, "weather?", [TOOL])]
    calls = [e.tool_use for e in events if e.event_type == "tool_use"]
    assert len(calls) == 2
    assert all("thought_signature" not in c for c in calls)  # not leaked to callers
    stored = {
        b.tool_input["city"]: b.provider_metadata
        async for b in MessageBlock.objects.filter(block_type="tool_use")
    }
    assert stored == {
        "Oslo": {"google": {"thought_signature": "SIG-A"}},
        "Rome": None,
    }
    results = [(c["id"], "sunny", False) for c in calls]
    events = [
        e async for e in gemini3.submit_tool_results_batch(session, results, [TOOL])
    ]
    # the second request replayed the first reply's signature unchanged
    first_turn = _model_parts(rec)
    assert first_turn[0]["thoughtSignature"] == "SIG-A"
    assert "thoughtSignature" not in first_turn[1]
    call = next(e.tool_use for e in events if e.event_type == "tool_use")
    [
        e
        async for e in gemini3.submit_tool_results_batch(
            session, [(call["id"], "rainy", False)], [TOOL]
        )
    ]
    # and the third request carries both turns' signatures, in order
    sent = [p for c in rec.body["contents"] if c["role"] == "model" for p in c["parts"]]
    assert [p.get("thoughtSignature") for p in sent] == ["SIG-A", None, "SIG-B"]


@pytest.mark.django_db(transaction=True)
async def test_missing_signature_falls_back_to_the_documented_dummy(
    mock, gemini3, session
):
    rec = mock(
        candidate([{"functionCall": {"name": "get_weather", "args": {"city": "X"}}}]),
        candidate([{"text": "ok"}]),
    )
    events = [e async for e in gemini3.send(session, "weather?", [TOOL])]
    call = next(e.tool_use for e in events if e.event_type == "tool_use")
    block = await MessageBlock.objects.aget(block_type="tool_use")
    assert block.provider_metadata is None
    [
        e
        async for e in gemini3.submit_tool_results_batch(
            session, [(call["id"], "r", False)], [TOOL]
        )
    ]
    assert _model_parts(rec)[0]["thoughtSignature"] == wire.DUMMY_SIGNATURE


@pytest.mark.django_db(transaction=True)
async def test_a_stored_signature_is_never_replaced_by_the_dummy(
    mock, gemini3, session
):
    rec = mock(
        candidate(
            [
                {
                    "functionCall": {"name": "get_weather", "args": {}},
                    "thoughtSignature": "REAL",
                }
            ]
        ),
        candidate([{"text": "ok"}]),
    )
    events = [e async for e in gemini3.send(session, "x", [TOOL])]
    call = next(e.tool_use for e in events if e.event_type == "tool_use")
    [
        e
        async for e in gemini3.submit_tool_results_batch(
            session, [(call["id"], "r", False)], [TOOL]
        )
    ]
    assert _model_parts(rec)[0]["thoughtSignature"] == "REAL"


@pytest.mark.django_db(transaction=True)
async def test_other_engines_ignore_a_stored_signature(session):
    from django_ergo.conversation.engines.claude_api import claude_message_dict
    from django_ergo.conversation.engines.openai_api import openai_message_dicts
    from django_ergo.conversation.messages import add_message
    from django_ergo.conversation.messages import tool_use_block

    await add_message(
        session,
        "assistant",
        [
            tool_use_block(
                "c1",
                "get_weather",
                {"city": "Oslo"},
                {"google": {"thought_signature": "SIG"}},
            )
        ],
        stop_reason="tool_use",
    )
    msg = await session.messages.prefetch_related("content_blocks").aget()

    def render():
        return (
            openai_message_dicts(msg),
            claude_message_dict(msg),
            openai_message_dicts(msg, signatures=True),
        )

    from asgiref.sync import sync_to_async

    openai, claude, google = await sync_to_async(render)()
    assert "SIG" not in json.dumps([openai, claude])
    assert "thought_signature" not in openai[0]["tool_calls"][0]
    assert google[0]["tool_calls"][0]["thought_signature"] == "SIG"
    assert not getattr(OpenAIAPIEngine, "replay_signatures", False)
    assert GoogleAPIEngine.replay_signatures is True


def test_google_is_an_engine_type():
    assert EngineType.GOOGLE == "google"
    assert "google" in EngineType.values
    field = ConversationSession._meta.get_field("engine_type")
    assert field.clean("google", None) == "google"  # the choices accept it


# -- telemetry table ----------------------------------------------------------------------
# (cost accounting of finished calls only; never spend authority)


def test_price_rows_match_the_pricing_page_read_on_2026_11_25():
    rows = {
        "gemini-2.5-pro": (1.25, 10.00, 0.125),
        "gemini-2.5-flash": (0.30, 2.50, 0.03),
        "gemini-2.5-flash-lite": (0.10, 0.40, 0.01),
        "gemini-3.1-pro-preview": (2.00, 12.00, 0.20),
        "gemini-3.1-flash-lite": (0.25, 1.50, 0.025),
        "gemini-3-flash-preview": (0.50, 3.00, 0.05),
    }
    for model, (inp, out, cached) in rows.items():
        price = price_for(model)
        assert (price.input, price.output, price.cache_read) == (inp, out, cached)
    # >200k-token tier: input 4.00 / output 18.00 on 3.1 Pro, 2.50 / 15.00 on 2.5 Pro
    assert price_for("gemini-3.1-pro-preview").long_context == {
        "threshold": 200_000,
        "input": 2,
        "output": 1.5,
    }
    # Google lists gemini-3-pro-preview as shut down (2026-03-09): no price here
    assert price_for("gemini-3-pro-preview") is None


@pytest.mark.skipif(
    os.environ.get("ERGO_LIVE_GEMINI") != "1",
    reason="live Gemini call; set ERGO_LIVE_GEMINI=1 to opt in (costs credits)",
)
async def test_live_smoke():  # pragma: no cover
    engine = GoogleAPIEngine({"model": "gemini-2.5-flash-lite", "max_tokens": 32})
    result = await engine.complete([{"role": "user", "content": "Say ok."}])
    assert result.message["content"]
