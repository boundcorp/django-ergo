"""Fail-closed OpenAI text proxy. Install URLs only in the trusted proxy service.

The resolver is trusted service code: it returns a freshly revalidated
MeteredGuard, the service-only API key, and a non-secret account identifier.
It MUST reconstruct current SendAuthority from current bot configuration, not
from the launch snapshot. Neither credentials nor request bodies are persisted.
"""

# Internal guard primitives are shared intentionally within conversation.
# ruff: noqa: SLF001
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import timedelta
from decimal import ROUND_CEILING
from decimal import Decimal
from types import SimpleNamespace

import httpx
from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from django.http import StreamingHttpResponse
from django.utils import timezone
from django.utils.module_loading import import_string
from django.views.decorators.csrf import csrf_exempt

from django_ergo.conversation import spend_guard as guardlib
from django_ergo.conversation.models import InferenceOutcome
from django_ergo.conversation.models import InferenceRequest
from django_ergo.conversation.models import PaidAgentLaunch

MAX_BODY = 2_000_000
MAX_RESPONSE = 8_000_000
MAX_TTL = 3600
MAX_ATTEMPTS = 100
MIN_TOKEN_LENGTH = 20
MAX_TOKEN_LENGTH = 200
MAX_ACCOUNT_REF = 200
USD_QUANTUM = Decimal("0.0000000001")
UPSTREAM = "https://api.openai.com/v1"


def deny(reason):
    raise guardlib.SpendDenied(reason)


def namespace(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{k: namespace(v) for k, v in value.items()})
    return value


class ResponsesAdapter:
    """Bound only self-contained text and local function tools, with no stored context."""

    allowed = frozenset(
        {
            "model",
            "input",
            "instructions",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "max_output_tokens",
            "stream",
            "store",
            "metadata",
            "reasoning",
            "text",
            "temperature",
            "top_p",
            "truncation",
            "client_metadata",
            "prompt_cache_key",
            "include",
        }
    )

    def bound(self, payload, ceiling):
        guardlib._unsupported_keys(payload, self.allowed)
        self._tools(payload.get("tools") or [])
        choice = payload.get("tool_choice", "auto")
        if not (
            (isinstance(choice, str) and choice in {"auto", "none", "required"})
            or (
                isinstance(choice, dict)
                and choice.get("type") in {"function", "custom"}
            )
        ):
            deny("Only local tool choices are supported")
        # This selector requests opaque reasoning state, not another priced
        # capability. Omit it so subsequent turns cannot replay hidden input.
        include = payload.pop("include", [])
        if include not in ([], ["reasoning.encrypted_content"]):
            deny("Unsupported response expansion")
        self._text(payload.get("input", ""))
        if payload.get("truncation", "disabled") != "disabled":
            deny("Automatic context truncation is unsupported")
        payload["store"] = False
        output = guardlib._clamp(payload, "max_output_tokens", ceiling)
        # Entire serialized body plus generous per-item framing, including tool schemas.
        size = len(json.dumps(payload, ensure_ascii=True).encode())
        return guardlib.RequestBound(size * 2 + 1024, output, cache_write=False)

    def _tools(self, tools):
        if not isinstance(tools, list):
            deny("Tools must be explicit local definitions")
        for tool in tools:
            if not isinstance(tool, dict):
                deny("Malformed tool definition")
            if tool.get("type") == "namespace":
                self._tools(tool.get("tools"))
            elif tool.get("type") not in {"function", "custom"}:
                deny("Only client-executed function/custom tools are priced")

    def _text(self, value):
        if isinstance(value, str):
            return
        if not isinstance(value, list):
            deny("Input must contain self-contained text")
        allowed = {
            "message",
            "input_text",
            "output_text",
            "function_call",
            "function_call_output",
            "reasoning",
            "additional_tools",
            "custom_tool_call",
            "custom_tool_call_output",
        }
        for item in value:
            if not isinstance(item, dict) or item.get("type", "message") not in allowed:
                deny("Unsupported input modality")
            if item.get("encrypted_content"):
                deny("Encrypted context cannot be bounded")
            if item.get("type") == "additional_tools":
                self._tools(item.get("tools"))
            elif item.get("type", "message") == "message" and "content" not in item:
                deny("A message reference without content cannot be bounded")
            # Full inline items are self-contained; discard ids so the provider
            # cannot resolve a stored item instead of the explicit text/schema.
            item.pop("id", None)
            if "content" in item:
                self._text(item["content"])
            if isinstance(item.get("output"), list):
                self._text(item["output"])

    def usage(self, data):
        usage = data.get("usage")
        terminal = data.get("status") == "completed" or (
            data.get("status") == "incomplete"
            and data.get("incomplete_details") == {"reason": "max_output_tokens"}
        )
        if not isinstance(usage, dict) or not terminal:
            return None
        # Reuse the strict Chat usage parser, including cached and reasoning buckets.
        return guardlib.OpenAIChatAdapter().usage(
            namespace(
                {
                    "usage": {
                        "prompt_tokens": usage.get("input_tokens"),
                        "completion_tokens": usage.get("output_tokens"),
                        "total_tokens": usage.get("total_tokens"),
                        "prompt_tokens_details": usage.get("input_tokens_details"),
                        "completion_tokens_details": usage.get("output_tokens_details"),
                    }
                }
            )
        )


def issue(  # noqa: PLR0913 - explicit capability bindings
    *,
    guard,
    reservation_id,
    account_ref,
    authority,
    max_attempts=8,
    ttl=900,
    cap_usd=None,
):
    """Exchange an unsent launch allocation for per-attempt accounting atomically.

    The released allocation is informational, never a monetary spend. Actual
    attempts compete for remaining family authority; no second launch-level hold
    is added to their costs. The launch additionally caps SUM(reservations), not
    just settlements, so cheap completions cannot buy unlimited retries.
    """
    if not account_ref or guard.provider != "openai" or not guard.current:
        deny("Proxy requires OpenAI account-bound current authority")
    if not 0 < ttl <= MAX_TTL or not 0 < max_attempts <= MAX_ATTEMPTS:
        deny("Invalid launch limits")
    from django_ergo.conversation import weekly_budgets as weekly

    token = secrets.token_urlsafe(32)
    with transaction.atomic():
        # Follow #407: provider family, grant envelopes, then source row.
        weekly.lock_family(
            weekly.family_of(getattr(guard.current, "identity", ""))
            if getattr(guard.current, "identity", "")
            else guard.provider
        )
        envelope = guardlib._envelope_of(guard.grant_id)
        for key in guardlib._locks_of(envelope):
            guardlib.lock(key)
        allocation = InferenceRequest.objects.select_for_update().get(pk=reservation_id)
        if (
            allocation.user_id != guard.user_id
            or allocation.pool != guardlib.ledger_key(guard.grant_id)
            or allocation.provider != guard.provider
            or allocation.model != guard.model
            or allocation.purpose != "coding_launch"
            or allocation.outcome != "pending"
            or not allocation.reserved_usd
            or allocation.paid_launch_id
            or (
                allocation.account_mapping_id
                and str(allocation.account_mapping_id) != account_ref
            )
        ):
            deny("Launch allocation does not match proxy authority")
        cap = (
            min(allocation.reserved_usd, cap_usd)
            if cap_usd is not None
            else allocation.reserved_usd
        )
        if not cap.is_finite() or cap <= 0:
            deny("Launch cap must be positive and finite")
        launch = PaidAgentLaunch.objects.create(
            budget_week=allocation.budget_week,
            token_digest=hashlib.sha256(token.encode()).hexdigest(),
            user_id=guard.user_id,
            grant_id=guard.grant_id,
            reservation=allocation,
            provider=guard.provider,
            account_ref=account_ref,
            allowed_models=[guard.model],
            authority=authority,
            expires_at=min(
                timezone.now() + timedelta(seconds=ttl),
                allocation.created_at + timedelta(seconds=MAX_TTL),
            ),
            max_attempts=max_attempts,
            cap_usd=cap,
        )
        guardlib._finish(
            allocation.pk,
            outcome=InferenceOutcome.NOT_SENT,
            cost_status="not_sent",
            cost_usd=Decimal(0),
        )
    return launch, token


def revoke(launch_id):
    PaidAgentLaunch.objects.filter(pk=launch_id, revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )


def authenticate(token):
    if (
        not isinstance(token, str)
        or not MIN_TOKEN_LENGTH <= len(token) <= MAX_TOKEN_LENGTH
    ):
        deny("Invalid launch token")
    launch = PaidAgentLaunch.objects.filter(
        token_digest=hashlib.sha256(token.encode()).hexdigest()
    ).first()
    if launch is None or launch.revoked_at or launch.expires_at <= timezone.now():
        deny("Expired or revoked launch token")
    return launch


class Proxy:
    def __init__(self, resolver, client=None):
        self.resolver = resolver
        # No environment proxy, redirect or implicit retry can choose another account/host.
        self.client = client or httpx.Client(
            timeout=120, trust_env=False, follow_redirects=False
        )

    def begin(self, token, path, payload):  # noqa: C901, PLR0912 - admission checks stay before transport
        launch = authenticate(token)
        from django_ergo.conversation.weekly_budgets import week_of

        if launch.budget_week and launch.budget_week != week_of():
            deny("Launch belongs to an earlier UTC budget week")
        if path not in {"responses", "chat/completions"} or not isinstance(
            payload, dict
        ):
            deny("Unsupported endpoint or request")
        if len(json.dumps(payload).encode()) > MAX_BODY:
            deny("Request too large")
        guard, key, account = self.resolver(launch)
        if (
            not key
            or not guard.current
            or guard.provider != launch.provider
            or guard.grant_id != launch.grant_id
            or guard.user_id != launch.user_id
            or account != launch.account_ref
            or payload.get("model") not in launch.allowed_models
            or payload.get("model") != guard.model
        ):
            deny("Launch/account/model authority differs")
        adapter = (
            ResponsesAdapter() if path == "responses" else guardlib.OpenAIChatAdapter()
        )
        if path == "chat/completions":
            # Tighten the existing adapter: reject opaque non-text message fields.
            allowed = {
                "role",
                "content",
                "name",
                "tool_calls",
                "tool_call_id",
                "refusal",
            }
            for message in payload.get("messages") or []:
                if not isinstance(message, dict) or set(message) - allowed:
                    deny("Unsupported chat message")
                if message.get("content") is not None and not isinstance(
                    message["content"], str | list
                ):
                    deny("Unsupported chat content")
            if payload.get("stream"):
                payload["stream_options"] = {"include_usage": True}
        refresh = getattr(guard.current, "refresh", None)
        if callable(refresh):
            refresh()
        terms = guardlib.authorize(
            grant_id=guard.grant_id, user_id=guard.user_id, current=guard.current
        )
        ceiling = min(guard.max_output_tokens, terms.max_output_tokens)
        price = (
            terms.prices.get(f"{guard.provider}/{guard.model}")
            if terms.prices is not None
            else guard.price
        )
        bound = adapter.bound(payload, ceiling)
        if bound.output_tokens > ceiling or price is None:
            deny("Current output ceiling or tariff cannot cover request")
        input_bound = max(
            bound.input_tokens,
            len(json.dumps(payload, ensure_ascii=True).encode()) * 2 + 1024,
        )
        amount = price.worst_case(input_bound, bound.output_tokens, cache_write=False)
        if amount is None:
            deny("Request tariff cannot be bounded")
        amount = amount.quantize(USD_QUANTUM, rounding=ROUND_CEILING)
        with transaction.atomic():
            launch = PaidAgentLaunch.objects.select_for_update().get(pk=launch.pk)
            if launch.revoked_at or launch.expires_at <= timezone.now():
                deny("Expired or revoked launch token")
            if (
                launch.attempts >= launch.max_attempts
                or launch.total_reserved_usd + amount > launch.cap_usd
            ):
                deny("Launch cap exhausted")
            pk = guardlib.reserve(
                grant_id=guard.grant_id,
                provider=guard.provider,
                model=guard.model,
                amount=amount,
                user_id=guard.user_id,
                current=guard.current,
                output_tokens=bound.output_tokens,
                price=price,
                fields={
                    "user_id": guard.user_id,
                    "session_id": guard.session_id,
                    "bot_name": guard.bot,
                    "selector": f"{guard.provider}/{guard.model}",
                    "purpose": "paid_agent",
                    "paid_launch": launch,
                    "attempt": launch.attempts + 1,
                    "rates": price.snapshot(),
                },
            )
            launch.attempts += 1
            launch.total_reserved_usd += amount
            if (
                launch.attempts == launch.max_attempts
                or launch.total_reserved_usd >= launch.cap_usd
            ):
                launch.revoked_at = timezone.now()
            launch.save(update_fields=["attempts", "total_reserved_usd", "revoked_at"])
        return launch, pk, adapter, price, key

    def request(self, token, path, payload):
        try:
            launch, pk, adapter, price, key = self.begin(token, path, payload)
        except guardlib.SpendDenied:
            # Revoke even when the failed reservation rolled its transaction back.
            candidate = PaidAgentLaunch.objects.filter(
                token_digest=hashlib.sha256(token.encode()).hexdigest()
            ).first()
            if candidate:
                revoke(candidate.pk)
            raise
        try:
            req = self.client.build_request(
                "POST",
                f"{UPSTREAM}/{path}",
                json=payload,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
            )
            response = self.client.send(req, stream=True)
        except BaseException:
            guardlib._finish(
                pk, outcome=InferenceOutcome.ERROR, cost_status="unknown", cost_usd=None
            )
            raise
        # Record header evidence immediately, even if the body later disconnects.
        request_id = response.headers.get("x-request-id", "")[:200]
        InferenceRequest.objects.filter(pk=pk).update(upstream_request_id=request_id)
        if payload.get("stream") and response.is_success:
            return StreamingHttpResponse(
                StreamBody(
                    self.stream(response, launch, pk, adapter, price, path),
                    response,
                    launch.pk,
                    pk,
                ),
                content_type="text/event-stream",
            )
        try:
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_RESPONSE:
                    deny("Upstream response too large")
            data = json.loads(body)
            self.settle(launch, pk, adapter, price, path, data, response.is_success)
            # Do not forward upstream error text: it may contain credentials or internals.
            if not response.is_success:
                return JsonResponse(
                    {"error": "Upstream request failed; reservation retained"},
                    status=502,
                )
            return JsonResponse(data, safe=False)
        except BaseException:
            guardlib._finish(
                pk, outcome=InferenceOutcome.ERROR, cost_status="unknown", cost_usd=None
            )
            raise
        finally:
            response.close()

    def capture(self, pk, path, data):
        """Retain non-secret provider evidence even when final usage never arrives."""
        if not isinstance(data, dict):
            return
        fields = {}
        if isinstance(data.get("model"), str) and data["model"]:
            fields["actual_model"] = data["model"][:200]
        usage = data.get("usage")
        if isinstance(usage, dict):
            names = {
                "prompt_tokens": "input_tokens"
                if path == "responses"
                else "prompt_tokens",
                "output_tokens": "output_tokens"
                if path == "responses"
                else "completion_tokens",
            }
            for field, source in names.items():
                value = usage.get(source)
                if (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 0
                ):
                    fields[field] = value
        if fields:
            InferenceRequest.objects.filter(pk=pk).update(**fields)

    def settle(self, launch, pk, adapter, price, path, data, complete):  # noqa: PLR0913 - one attempt context
        self.capture(pk, path, data)
        actual = data.get("model", "")
        usage = (
            adapter.usage(data)
            if path == "responses"
            else adapter.usage(namespace(data))
        )
        row = InferenceRequest.objects.get(pk=pk)
        fields = guardlib.priced(usage, price)
        if (
            not complete
            or (usage is not None and usage.prompt <= 0)
            or actual != row.model
            or data.get("service_tier") not in (None, "default")
        ):
            fields.update(cost_usd=None, cost_status="unknown")
        usage_data = data.get("usage") or {}
        input_details = (
            usage_data.get(
                "input_tokens_details"
                if path == "responses"
                else "prompt_tokens_details"
            )
            or {}
        )
        if any(
            value not in (None, 0)
            for key, value in input_details.items()
            if key != "cached_tokens"
        ):
            fields.update(cost_usd=None, cost_status="unknown")
        detail_key = (
            "output_tokens_details"
            if path == "responses"
            else "completion_tokens_details"
        )
        reasoning = (data.get("usage") or {}).get(detail_key) or {}
        reasoning = reasoning.get("reasoning_tokens")
        if (
            isinstance(reasoning, int)
            and not isinstance(reasoning, bool)
            and 0 <= reasoning <= (fields.get("output_tokens") or 0)
        ):
            fields["reasoning_tokens"] = reasoning
        # Provider overrun or identity change is never represented as a released hold.
        cost = fields.get("cost_usd")
        if cost is not None and (
            cost > row.reserved_usd or cost != cost.quantize(USD_QUANTUM)
        ):
            fields.update(cost_usd=None, cost_status="unknown")
        if fields["cost_status"] == "unknown":
            revoke(launch.pk)
        guardlib._finish(
            pk,
            outcome=InferenceOutcome.OK if complete else InferenceOutcome.ERROR,
            actual_model=str(actual)[:200] if actual else row.actual_model,
            **fields,
        )

    def stream(self, response, launch, pk, adapter, price, path):  # noqa: PLR0913, C901 -- terminal evidence and disconnect checks
        terminal = None
        done = False
        size = 0
        try:
            for line in response.iter_lines():
                size += len(line)
                if size > MAX_RESPONSE:
                    deny("Upstream stream too large")
                if line.startswith("data:"):
                    value = line[5:].strip()
                    if value == "[DONE]":
                        done = True
                    else:
                        if done or terminal is not None:
                            deny("Data after terminal usage is ambiguous")
                        event = json.loads(value)
                        self.capture(pk, path, event.get("response") or event)
                        if path == "responses" and event.get("type") in {
                            "response.completed",
                            "response.incomplete",
                        }:
                            if terminal is not None:
                                deny("Ambiguous duplicate terminal usage")
                            candidate = event.get("response")
                            expected = event["type"].removeprefix("response.")
                            if (
                                not isinstance(candidate, dict)
                                or candidate.get("status") != expected
                            ):
                                deny("Terminal event/status mismatch")
                            terminal = candidate
                        elif path == "chat/completions" and event.get("usage"):
                            terminal = event
                yield (line + "\n").encode()
            complete = terminal is not None and (path == "responses" or done)
            self.settle(launch, pk, adapter, price, path, terminal or {}, complete)
        except BaseException:
            revoke(launch.pk)
            guardlib._finish(
                pk, outcome=InferenceOutcome.ERROR, cost_status="unknown", cost_usd=None
            )
            raise
        finally:
            response.close()


@csrf_exempt
def proxy_view(request, endpoint):
    """Mounted on a separate service with a service-owned resolver and credentials."""
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    authorization = request.headers.get("Authorization", "")
    token = (
        authorization.removeprefix("Bearer ")
        if authorization.startswith("Bearer ")
        else ""
    )
    try:
        if endpoint == "revoke":
            launch = authenticate(token)
            revoke(launch.pk)
            return JsonResponse({"revoked": True})
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            deny("Request too large")
        resolver = getattr(settings, "PAID_AGENT_PROXY_RESOLVER", "")
        if not resolver:
            deny("Proxy authority resolver is not configured")
        payload = json.loads(request.body)
        proxy = Proxy(import_string(resolver))
        try:
            result = proxy.request(token, endpoint, payload)
            if isinstance(result, StreamingHttpResponse):
                result._resource_closers.append(proxy.client.close)
            else:
                proxy.client.close()
            return result
        except BaseException:
            proxy.client.close()
            raise
    except (guardlib.SpendDenied, guardlib.UnsupportedRequest, ValueError, TypeError):
        return JsonResponse({"error": "Paid request refused"}, status=403)
    except Exception:  # noqa: BLE001 - never expose transport/key/response details
        return JsonResponse(
            {"error": "Proxy failed; reservation retained if sent"}, status=502
        )


class StreamBody:
    """Django closes this even if the downstream never starts reading."""

    def __init__(self, iterator, response, launch_id, pk):
        self.iterator = iterator
        self.response = response
        self.launch_id = launch_id
        self.pk = pk

    def __iter__(self):
        return self.iterator

    def close(self):
        self.iterator.close()
        self.response.close()
        if InferenceRequest.objects.filter(pk=self.pk, outcome="pending").exists():
            revoke(self.launch_id)
            guardlib._finish(
                self.pk,
                outcome=InferenceOutcome.ERROR,
                cost_status="unknown",
                cost_usd=None,
            )


def mapped_account(identity):
    """Operator-audited non-secret org/project/key-id mapping, never a key value."""
    from django_ergo.conversation.models import ProviderAccountMapping
    from django_ergo.conversation.weekly_budgets import account_identity

    mapping = ProviderAccountMapping.objects.filter(
        identity=account_identity(identity), family_id="openai"
    ).first()
    if mapping is None:
        deny("No audited OpenAI account mapping for this transport")
    # The immutable mapping row, also stamped on attempts, is the identity.
    return str(mapping.pk)
