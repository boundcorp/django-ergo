"""Fireworks API engine for private pools (``private_pools`` in providers.yaml).

It speaks the OpenAI-compatible Chat Completions protocol, so it reuses
:class:`OpenAIAPIEngine`'s message, tool and usage adapters, with a fixed
identity no config can change:

- the endpoint is always ``https://api.fireworks.ai/inference/v1``;
- the API key is read from ``FIREWORKS_API_KEY`` at send time, inside the
  HTTP transport, and is never stored on the engine, in config dicts, session
  metadata, logs, reprs or exceptions;
- the model must be the exact model ID of the engine's grant.

Every model request (session turns, tool continuations, ``complete``,
``generate``) goes through :meth:`FireworksAPIEngine._create_completion`,
which checks the engine's :class:`~inference_policy.PrivateGrant` before it
reads the key or opens a connection, records an ``InferenceRequest`` ledger
row (reserving the worst-case cost against the pool's cap) and allows exactly
one HTTP attempt. The SDK's own retries are off; the structured call's retry
of transient failures is the only retry, on the same model, and each of its
attempts is checked and recorded again. The transport refuses any request
outside such an attempt, so reusing the cached client directly fails too.
"""

from __future__ import annotations

import json
import os
import weakref
from contextvars import ContextVar
from dataclasses import dataclass

import httpx

from django_ergo.conversation.engines.openai_api import OpenAIAPIEngine
from django_ergo.conversation.inference_policy import FIREWORKS_BASE_URL
from django_ergo.conversation.inference_policy import FIREWORKS_HOST
from django_ergo.conversation.inference_policy import FIREWORKS_KEY_ENV
from django_ergo.conversation.inference_policy import PRIVATE_RESERVED_CONFIG
from django_ergo.conversation.inference_policy import PolicyError
from django_ergo.conversation.inference_policy import PrivateGrant
from django_ergo.conversation.inference_policy import check_grant

CHAT_PATH = "/inference/v1/chat/completions"
# Stands in for the key in the SDK client; the transport replaces the header.
_KEY_PLACEHOLDER = "set-per-request-by-the-guarded-transport"
# Headers the OpenAI SDK may add from OPENAI_* env that Fireworks never gets.
_DROP_HEADERS = ("openai-organization", "openai-project")
REQUEST_TIMEOUT = httpx.Timeout(600.0, connect=15.0)


@dataclass
class _Attempt:
    engine: FireworksAPIEngine
    grant: PrivateGrant
    model_id: str
    http_requests: int = 0


_active: ContextVar[_Attempt | None] = ContextVar(
    "ergo_fireworks_attempt", default=None
)


def _inner_transport() -> httpx.AsyncBaseTransport:
    """The real network transport (tests replace it with httpx.MockTransport)."""
    return httpx.AsyncHTTPTransport()


class _GuardedTransport(httpx.AsyncBaseTransport):
    """Sends a request only inside its engine's guarded attempt, only to the
    fixed chat completions URL, only for the grant's model, once."""

    def __init__(self, engine: FireworksAPIEngine):
        self._engine = weakref.ref(engine)
        self._inner = _inner_transport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        attempt = _active.get()
        if attempt is None or attempt.engine is not self._engine():
            msg = (
                "Fireworks requests are only sent by the private engine's guarded send"
            )
            raise PolicyError(msg)
        check_grant(attempt.grant, model_id=attempt.model_id)
        url = request.url
        if (
            url.scheme != "https"
            or url.host != FIREWORKS_HOST
            or url.port not in (None, 443)
            or url.path != CHAT_PATH
        ):
            msg = "The private engine only sends to the fixed Fireworks chat endpoint"
            raise PolicyError(msg)
        try:
            body = json.loads(request.content or b"{}")
        except ValueError as exc:
            msg = "The private engine only sends JSON chat requests"
            raise PolicyError(msg) from exc
        if not isinstance(body, dict) or body.get("model") != attempt.model_id:
            msg = "The request's model isn't the one the private grant allows"
            raise PolicyError(msg)
        if attempt.http_requests:
            msg = "One HTTP request per guarded attempt (no hidden retries)"
            raise PolicyError(msg)
        key = os.environ.get(FIREWORKS_KEY_ENV, "")
        if not key:
            msg = f"{FIREWORKS_KEY_ENV} isn't set; the private pool can't send"
            raise PolicyError(msg)
        for name in _DROP_HEADERS:
            request.headers.pop(name, None)
        request.headers["Authorization"] = f"Bearer {key}"
        del key
        attempt.http_requests += 1
        try:
            return await self._inner.handle_async_request(request)
        finally:
            # The response keeps a reference to its request; don't leave the key on it.
            request.headers["Authorization"] = "Bearer [redacted]"

    async def aclose(self) -> None:
        await self._inner.aclose()


class FireworksAPIEngine(OpenAIAPIEngine):
    """A private pool member on Fireworks (see the module docstring)."""

    engine_type = "fireworks"
    transport_type = "api"

    def __init__(self, config: dict):
        config = dict(config or {})
        if bad := sorted(PRIVATE_RESERVED_CONFIG & set(config)):
            msg = (
                f"The private engine's {', '.join(bad)} can't be configured: the "
                "endpoint, key and model come from the private pool"
            )
            raise PolicyError(msg)
        model = config.get("model")
        if not isinstance(model, str) or not model:
            msg = "The private engine needs the exact model ID of a private pool member"
            raise PolicyError(msg)
        super().__init__(config)
        self.api_key = None
        self.base_url = FIREWORKS_BASE_URL
        self._grant: PrivateGrant | None = None
        self._attempts = 0

    def __repr__(self) -> str:
        return f"<FireworksAPIEngine {self.model}>"

    def authorize(self, grant: PrivateGrant) -> None:
        """Attach the grant trusted resolution code minted for this engine."""
        self._grant = check_grant(grant, model_id=self.model)

    @property
    def grant(self) -> PrivateGrant | None:
        return self._grant

    def _get_client(self):
        if self._client is None:
            import openai

            self._client = openai.AsyncOpenAI(
                api_key=_KEY_PLACEHOLDER,
                base_url=FIREWORKS_BASE_URL,
                max_retries=0,
                timeout=REQUEST_TIMEOUT,
                http_client=httpx.AsyncClient(
                    transport=_GuardedTransport(self),
                    follow_redirects=False,
                    timeout=REQUEST_TIMEOUT,
                ),
            )
        return self._client

    async def _create_completion(self, kwargs: dict):
        """One checked, recorded attempt: grant, then key, then cap, then HTTP."""
        from django_ergo.conversation import inference_ledger

        model_id = kwargs.get("model")
        grant = check_grant(self._grant, model_id=model_id)
        if kwargs.get("stream"):
            msg = "The private engine doesn't stream"
            raise PolicyError(msg)
        if not os.environ.get(FIREWORKS_KEY_ENV):
            msg = f"{FIREWORKS_KEY_ENV} isn't set; the private pool can't send"
            raise PolicyError(msg)
        self._attempts += 1
        entry = await inference_ledger.open_attempt(
            grant, kwargs, attempt=self._attempts
        )
        attempt = _Attempt(engine=self, grant=grant, model_id=model_id)
        token = _active.set(attempt)
        try:
            response = await self._get_client().chat.completions.create(**kwargs)
        except BaseException as exc:
            await inference_ledger.fail_attempt(
                entry, exc, sent=bool(attempt.http_requests)
            )
            # The SDK wraps transport errors (APIConnectionError, which is
            # retried); a policy refusal must surface as itself.
            policy = _policy_cause(exc)
            if policy is not None and policy is not exc:
                raise policy from None
            raise
        finally:
            _active.reset(token)
        await inference_ledger.finish_attempt(entry, response)
        return response


def _policy_cause(exc: BaseException | None) -> PolicyError | None:
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, PolicyError):
            return exc
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return None
