"""Google Gemini API engine (provider type ``google``).

Speaks the Gemini REST API directly with ``httpx`` (already a dependency of the
bots and OpenAI extras), not the ``google-genai`` SDK: the engine needs one
endpoint family (``generateContent`` / ``streamGenerateContent``), the repo
already routes every other provider through a mockable HTTP client, and it
keeps the credential handling in one place (the key goes in the
``x-goog-api-key`` header, is read at send time, and is never stored on the
engine, in a URL, in session metadata or in an error message).

It stores and renders history like :class:`OpenAIAPIEngine` (so the model can
change between turns and tool schemas are the same), and converts to Gemini's
format at the wire (:mod:`google_wire`). Failures raise the typed errors of
:mod:`google_errors`; there are no hidden retries (the structured call retries
transient failures, and a budgeted turn never retries).

Config (``providers.yaml`` ``config:`` or a model's ``config:``)::

    model: gemini-2.5-pro
    max_tokens: 32000          # maxOutputTokens (thinking included)
    temperature: 0.7
    reasoning_effort: medium   # none|minimal|low|medium|high|xhigh -> thinking config
    thinking_level / thinking_budget   # set the thinking config directly
    include_thoughts: false    # store and show thought summaries
    stream: false              # use streamGenerateContent and merge the chunks
    timeout: 600               # seconds
    base_url: ...              # tests and proxies; must be a public host
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx

from django_ergo.conversation.budget import enforce_request_limits
from django_ergo.conversation.budget import with_deadline
from django_ergo.conversation.engine import Completion
from django_ergo.conversation.engine import EngineResponse
from django_ergo.conversation.engines import google_wire as wire
from django_ergo.conversation.engines.google_errors import APIConnectionError
from django_ergo.conversation.engines.google_errors import APITimeoutError
from django_ergo.conversation.engines.google_errors import AuthenticationError
from django_ergo.conversation.engines.google_errors import GoogleAPIError
from django_ergo.conversation.engines.google_spend import GoogleAdapter
from django_ergo.conversation.engines.openai_api import OpenAIAPIEngine
from django_ergo.conversation.inference_policy import assert_public_endpoint
from django_ergo.conversation.messages import add_message
from django_ergo.conversation.messages import tool_use_block
from django_ergo.conversation.telemetry import record_usage
from django_ergo.conversation.telemetry import trace_engine_call

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_CONTEXT_WINDOW = 1_000_000
DEFAULT_TIMEOUT = 600.0
CONNECT_TIMEOUT = 15.0


def _transport() -> httpx.AsyncBaseTransport | None:
    """The network transport; tests replace this with ``httpx.MockTransport``."""
    return None


def _tool_call(call: dict) -> dict:
    """A reply's call as an assistant message ``tool_calls`` entry."""
    return {
        "id": call["id"],
        "type": "function",
        "function": {"name": call["name"], "arguments": json.dumps(call["input"])},
        **(
            {"thought_signature": call["thought_signature"]}
            if call.get("thought_signature")
            else {}
        ),
    }


class GoogleAPIEngine(OpenAIAPIEngine):
    """Gemini models over the Gemini API (see the module docstring)."""

    engine_type = "google"
    transport_type = "api"
    supports_request_limits = True
    # History carries each tool call's stored thoughtSignature back to Gemini.
    replay_signatures = True
    # A metered pool route sends through ``spend_guard`` with this adapter
    # (google_spend): request bound from the wire body, strict usageMetadata.
    spend_adapter = GoogleAdapter()

    def __init__(self, config: dict):
        config = dict(config or {})
        self.config = config
        self.model = str(config.get("model") or DEFAULT_MODEL).removeprefix("models/")
        self.context_window = int(
            config.get("context_window") or DEFAULT_CONTEXT_WINDOW
        )
        self.api_key = config.get("api_key") or None  # tests; normally read from env
        self.api_key_env = str(config.get("api_key_env") or "")
        self.base_url = str(config.get("base_url") or wire.BASE_URL).rstrip("/")
        self.temperature = config.get("temperature", 0.7)
        self.max_tokens = config.get("max_tokens", 32000)
        self.reasoning_effort = config.get("reasoning_effort")
        self.audio_input = config.get("audio_input", False)
        self.timeout = float(config.get("timeout") or DEFAULT_TIMEOUT)
        self.stream = bool(config.get("stream", False))
        self.include_thoughts = bool(config.get("include_thoughts", False))
        self._client = None
        from django_ergo.conversation.adapters import OpenAIToolAdapter

        self._adapter = OpenAIToolAdapter()

    def __repr__(self) -> str:
        return f"<GoogleAPIEngine {self.model}>"

    # -- credential -------------------------------------------------------------

    @staticmethod
    def configured(env_name: str = "") -> bool:
        """Whether a Gemini key is set (GEMINI_API_KEY, else GOOGLE_API_KEY)."""
        return wire.configured(env_name)

    def _key(self) -> str:
        key = self.api_key or wire.api_key(self.api_key_env)
        if not key:
            msg = (
                "No Gemini API key: set GEMINI_API_KEY (or GOOGLE_API_KEY) "
                "in the environment"
            )
            raise AuthenticationError(msg, reason="MISSING_KEY")
        return key

    # -- wire -------------------------------------------------------------------

    def _request(self, messages: list[dict], **extra) -> dict:
        thinking = wire.thinking_config(
            self.model,
            self.reasoning_effort,
            level=self.config.get("thinking_level"),
            budget=self.config.get("thinking_budget"),
            include_thoughts=self.include_thoughts,
        )
        return wire.build_request(
            messages,
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            thinking=thinking or None,
            **extra,
        )

    def _url(self, *, stream: bool) -> str:
        method = "streamGenerateContent?alt=sse" if stream else "generateContent"
        model = quote(self.model, safe="")
        return f"{self.base_url}/{wire.API_VERSION}/models/{model}:{method}"

    async def _create_completion(self, body: dict) -> wire.Reply:
        """Send one request and return its reply. Every model request goes
        through here. With ``request_limits`` (a budgeted turn) the request is
        refused if its input could pass them, its output tokens (thinking
        included) are cut to what is left, and the time left is its timeout."""
        timeout = self.timeout
        limits = self.request_limits
        if limits is not None:
            probe = {
                "request": body,
                "maxOutputTokens": body.get("generationConfig", {}).get(
                    "maxOutputTokens"
                ),
            }
            options = enforce_request_limits(limits, probe, "maxOutputTokens")
            body.setdefault("generationConfig", {})["maxOutputTokens"] = probe[
                "maxOutputTokens"
            ]
            timeout = options["timeout"]
            data = await with_deadline(limits, self._attempt(body, timeout))
        else:
            data = await self._attempt(body, timeout)
        return wire.parse_response(data)

    async def _attempt(self, body: dict, timeout: float) -> dict:  # noqa: ASYNC109
        """ONE HTTP attempt, through the spend guard on a metered pool route:
        the guard bounds the request and reserves its worst-case cost before
        anything is sent, clamping ``maxOutputTokens`` in ``body``, and prices
        (or keeps the hold on) the response. A retry is another call here, so
        it gets its own reservation."""
        guard = getattr(self, "spend_guard", None)
        if guard is None:
            return await self._send(body, timeout)
        body.setdefault("generationConfig", {})  # shared with the guard's view
        return await guard.call(
            {"model": self.model, **body},
            GoogleAdapter(getattr(guard, "price", None)),
            lambda: self._send(body, timeout),
        )

    async def _send(self, body: dict, timeout: float) -> dict:  # noqa: ASYNC109
        key = self._key()
        url = self._url(stream=self.stream)
        assert_public_endpoint(url, what="The Gemini API engine")
        headers = {"x-goog-api-key": key, "content-type": "application/json"}
        client_timeout = httpx.Timeout(timeout, connect=min(CONNECT_TIMEOUT, timeout))
        try:
            async with httpx.AsyncClient(  # noqa: SIM117
                transport=_transport(),
                timeout=client_timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST", url, json=body, headers=headers
                ) as response:
                    if response.status_code >= 400:  # noqa: PLR2004
                        raw = (await response.aread()).decode("utf-8", "replace")
                        raise wire.error_for(
                            response.status_code,
                            _json_or_text(raw),
                            key=key,
                            retry_header=response.headers.get("retry-after", ""),
                        )
                    if self.stream:
                        return await self._read_stream(response, key)
                    return json.loads((await response.aread()) or b"{}")
        except GoogleAPIError:
            raise
        except (TimeoutError, httpx.TimeoutException) as exc:
            msg = f"Gemini request timed out after {timeout:g}s"
            raise APITimeoutError(msg, reason="TIMEOUT") from exc
        except httpx.TransportError as exc:
            raise wire.connection_error(exc, key) from exc
        except ValueError as exc:
            msg = "Gemini returned a response that isn't valid JSON"
            raise APIConnectionError(msg, reason="BAD_JSON") from exc

    async def _read_stream(self, response: httpx.Response, key: str) -> dict:
        chunks: list[dict] = []
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            chunk = json.loads(payload)
            if isinstance(chunk, dict) and "error" in chunk:
                error = chunk["error"] if isinstance(chunk["error"], dict) else {}
                raise wire.error_for(int(error.get("code") or 500), chunk, key=key)
            chunks.append(chunk)
        return wire.merge_stream(chunks)

    # -- session turns ------------------------------------------------------------

    async def _call(
        self, session, additional_tools: list[dict] | None = None
    ) -> AsyncIterator[EngineResponse]:
        """Rebuild history, call the API, persist the reply, and yield events."""
        with trace_engine_call(
            operation="send",
            engine_type=self.engine_type,
            model=self.model,
            session_id=str(session.id) if session else "",
            transport_type="api",
            max_tokens=self.max_tokens,
        ) as span:
            from asgiref.sync import sync_to_async

            messages = await sync_to_async(
                self.reconstruct_messages, thread_sensitive=True
            )(session)
            body = self._request(messages, tools=additional_tools or None)
            reply = await self._create_completion(body)
            record_usage(
                span,
                input_tokens=reply.usage["input_tokens"],
                output_tokens=reply.usage["output_tokens"],
                cache_read=reply.usage["cache_read_input_tokens"],
            )

            blocks: list[dict] = []
            if reply.thoughts and self.include_thoughts:
                blocks.append({"block_type": "thinking", "thinking": reply.thoughts})
            if reply.text:
                blocks.append({"block_type": "text", "text": reply.text})
            blocks += [
                tool_use_block(
                    c["id"],
                    c["name"],
                    c["input"],
                    {"google": {"thought_signature": c["thought_signature"]}}
                    if c.get("thought_signature")
                    else None,
                )
                for c in reply.calls
            ]
            if blocks:
                await add_message(
                    session,
                    "assistant",
                    blocks,
                    stop_reason="tool_use" if reply.calls else reply.finish_reason,
                    model_name=self.model,
                    **reply.usage,
                )

            if reply.thoughts and self.include_thoughts:
                yield EngineResponse(
                    event_type="thinking",
                    raw={"type": "thinking"},
                    thinking=reply.thoughts,
                )
            if reply.text:
                yield EngineResponse(event_type="text", raw={}, text=reply.text)
            for call in reply.calls:
                yield EngineResponse(
                    event_type="tool_use",
                    raw={"name": call["name"], "input": call["input"]},
                    tool_use={
                        k: v for k, v in call.items() if k != "thought_signature"
                    },
                )
            yield EngineResponse(event_type="done", raw=_done(reply))

    # -- sessionless calls ----------------------------------------------------------

    async def complete(
        self,
        messages: list[dict],
        *,
        system: str = "",
        tools: list[dict] | None = None,
    ) -> Completion:
        with trace_engine_call(
            operation="complete",
            engine_type=self.engine_type,
            model=self.model,
            transport_type="api",
            max_tokens=self.max_tokens,
        ) as span:
            body = self._request(messages, tools=tools or None, system=system)
            reply = await self._create_completion(body)
            record_usage(
                span,
                input_tokens=reply.usage["input_tokens"],
                output_tokens=reply.usage["output_tokens"],
                cache_read=reply.usage["cache_read_input_tokens"],
            )
        message: dict = {"role": "assistant", "content": reply.text or None}
        events: list[EngineResponse] = []
        if reply.text:
            events.append(EngineResponse(event_type="text", raw={}, text=reply.text))
        if reply.calls:
            message["tool_calls"] = [_tool_call(c) for c in reply.calls]
            events.extend(
                EngineResponse(
                    event_type="tool_use",
                    raw={"name": c["name"], "input": c["input"]},
                    tool_use={k: v for k, v in c.items() if k != "thought_signature"},
                )
                for c in reply.calls
            )
        events.append(EngineResponse(event_type="done", raw=_done(reply)))
        return Completion(
            message=message,
            events=events,
            model=self.model,
            **{k: v or 0 for k, v in reply.usage.items()},
        )

    async def generate(
        self,
        prompt: str,
        workflow=None,
        system: str | None = None,
        response_model: type | None = None,
    ) -> EngineResponse:
        """One-shot generation; with ``response_model``, JSON in its schema."""
        with trace_engine_call(
            operation="generate",
            engine_type=self.engine_type,
            model=self.model,
            transport_type="api",
            max_tokens=self.max_tokens,
        ) as span:
            sys_prompt = system or (workflow.instructions if workflow else None) or ""
            schema = response_model.model_json_schema() if response_model else None
            body = self._request(
                [{"role": "user", "content": prompt}],
                system=sys_prompt,
                response_schema=schema,
            )
            reply = await self._create_completion(body)
            record_usage(
                span,
                input_tokens=reply.usage["input_tokens"],
                output_tokens=reply.usage["output_tokens"],
            )
        usage = {
            "prompt_tokens": (reply.usage["input_tokens"] or 0)
            + (reply.usage["cache_read_input_tokens"] or 0),
            "completion_tokens": reply.usage["output_tokens"] or 0,
        }
        if response_model is not None:
            parsed = response_model.model_validate_json(reply.text or "{}")
            return EngineResponse(
                event_type="done", raw={"parsed": parsed, "usage": usage}
            )
        return EngineResponse(event_type="done", raw={"usage": usage}, text=reply.text)


def _done(reply: wire.Reply) -> dict:
    raw: dict = {"finish_reason": reply.finish_reason}
    if reply.google_finish:
        raw["google_finish_reason"] = reply.google_finish
    if reply.refusal_category:
        raw["refusal_category"] = reply.refusal_category
    return raw


def _json_or_text(raw: str):
    try:
        return json.loads(raw)
    except ValueError:
        return raw
