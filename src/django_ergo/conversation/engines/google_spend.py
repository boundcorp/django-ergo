"""The Gemini implementation of the spend-guard adapter contract
(:class:`django_ergo.conversation.spend_guard.SpendAdapter`).

A metered pool route sends a Gemini request only through
``MeteredGuard.call(kwargs, GoogleAdapter(price), send)``, where ``kwargs`` is
the ``generateContent`` wire body plus ``model`` (the model is in the URL, not
the body; the guard checks it against the route). The adapter

- refuses what it can't bound or price (non-text parts, server-side tools,
  cached-content references, several candidates, other response modalities,
  opaque options, and a prompt that may pass Gemini's long-context threshold
  when the tariff has no ``long_context`` rate);
- clamps ``generationConfig.maxOutputTokens`` to the pool's ceiling in place
  (it is set when absent; Gemini counts thinking tokens against it) and bounds
  the prompt by the JSON size of everything that is sent as input;
- parses ``usageMetadata`` strictly: cached prompt tokens are split out,
  ``thoughtsTokenCount`` is output, and a missing, malformed, negative or
  inconsistent count (or a non-text modality bill) returns None, which keeps
  the reservation (never a priced zero).

Prices come only from the route's exact ``price:`` in providers.yaml; the
telemetry table in ``pricing.DEFAULT_PRICES`` is never consulted here.
"""

from __future__ import annotations

from django_ergo.conversation.spend_guard import MESSAGE_OVERHEAD_TOKENS
from django_ergo.conversation.spend_guard import RequestBound
from django_ergo.conversation.spend_guard import UnsupportedRequest
from django_ergo.conversation.spend_guard import Usage
from django_ergo.conversation.spend_guard import _clamp
from django_ergo.conversation.spend_guard import _count
from django_ergo.conversation.spend_guard import _unsupported_keys

# Gemini's published long-context tier boundary (prompts over this many tokens
# are billed at higher rates on the models that have one). A request whose
# prompt bound passes it needs a ``long_context`` rate on the route's price.
LONG_CONTEXT_TOKENS = 200_000

REQUEST_KEYS = frozenset(
    {
        "model",
        "contents",
        "systemInstruction",
        "tools",
        "toolConfig",
        "generationConfig",
    }
)
GENERATION_KEYS = frozenset(
    {
        "maxOutputTokens",
        "temperature",
        "topP",
        "topK",
        "stopSequences",
        "thinkingConfig",
        "responseMimeType",
        "responseSchema",
    }
)
# Parts that are billed as plain text tokens.
PART_KEYS = frozenset(
    {"text", "functionCall", "functionResponse", "thought", "thoughtSignature"}
)
TOOL_KEYS = frozenset({"functionDeclarations"})


class GoogleAdapter:
    """Gemini ``generateContent`` / ``streamGenerateContent``."""

    output_key = "generationConfig.maxOutputTokens"

    def __init__(self, price=None):
        # The route's ExactPrice, for the long-context check only; the guard
        # prices the reservation itself.
        self.price = price

    def _check_parts(self, parts, where: str) -> None:
        for part in parts if isinstance(parts, list) else ():
            if not isinstance(part, dict):
                msg = f"{where} has a part that isn't an object"
                raise UnsupportedRequest(msg)
            if extra := sorted(set(part) - PART_KEYS):
                msg = (
                    f"{', '.join(extra)} input has no exact token bound; a metered "
                    "pool route only sends text and function calls"
                )
                raise UnsupportedRequest(msg)

    def bound(self, kwargs: dict, ceiling: int) -> RequestBound:  # noqa: C901
        from django_ergo.conversation.budget import input_upper_bound

        _unsupported_keys(kwargs, REQUEST_KEYS)
        contents = kwargs.get("contents")
        if not isinstance(contents, list) or not contents:
            msg = "the request has no contents"
            raise UnsupportedRequest(msg)
        for content in contents:
            if not isinstance(content, dict):
                msg = "a contents entry isn't an object"
                raise UnsupportedRequest(msg)
            if extra := sorted(set(content) - {"role", "parts"}):
                msg = f"contents field(s) {', '.join(extra)} aren't supported"
                raise UnsupportedRequest(msg)
            self._check_parts(content.get("parts"), "contents")
        system = kwargs.get("systemInstruction")
        if system is not None:
            if not isinstance(system, dict) or set(system) - {"role", "parts"}:
                msg = "systemInstruction must hold text parts only"
                raise UnsupportedRequest(msg)
            self._check_parts(system.get("parts"), "systemInstruction")
        for tool in kwargs.get("tools") or []:
            if not isinstance(tool, dict) or set(tool) - TOOL_KEYS:
                names = (
                    ", ".join(sorted(set(tool) - TOOL_KEYS))
                    if isinstance(tool, dict)
                    else "a non-object tool"
                )
                msg = (
                    f"server-side tool {names} can bill per-use fees the tariff "
                    "doesn't price; only function declarations are sent under "
                    "a spend guard"
                )
                raise UnsupportedRequest(msg)
        config = kwargs.get("generationConfig")
        if config is None:
            config = kwargs["generationConfig"] = {}
        if not isinstance(config, dict):
            msg = "generationConfig must be an object"
            raise UnsupportedRequest(msg)
        _unsupported_keys(config, GENERATION_KEYS)
        output = _clamp(config, "maxOutputTokens", ceiling)

        sent = {
            k: kwargs.get(k)
            for k in ("contents", "systemInstruction", "tools", "toolConfig")
        }
        sent["generationConfig"] = {
            k: v for k, v in config.items() if k != "maxOutputTokens"
        }
        tokens = input_upper_bound(sent) + MESSAGE_OVERHEAD_TOKENS * len(contents)
        if tokens > LONG_CONTEXT_TOKENS and (
            self.price is None or self.price.long_context is None
        ):
            msg = (
                f"the prompt could pass {LONG_CONTEXT_TOKENS} tokens (up to "
                f"{tokens}), where Gemini's long-context rates apply, and the "
                "route's price has no long_context rate"
            )
            raise UnsupportedRequest(msg)
        return RequestBound(tokens, output, cache_write=False)

    def usage(self, response) -> Usage | None:  # noqa: C901, PLR0911, PLR0912
        """Billed usage from a response's ``usageMetadata``, else None."""
        if not isinstance(response, dict):
            return None
        meta = response.get("usageMetadata")
        if not isinstance(meta, dict):
            return None
        prompt = _count(meta.get("promptTokenCount"))
        candidates = _count(meta.get("candidatesTokenCount"))
        if prompt is None or candidates is None:
            return None
        counts = {}
        for name in (
            "cachedContentTokenCount",
            "toolUsePromptTokenCount",
            "thoughtsTokenCount",
        ):
            if name in meta:
                value = _count(meta[name])
                if value is None:
                    return None
                counts[name] = value
        cached = counts.get("cachedContentTokenCount", 0)
        tool_use = counts.get("toolUsePromptTokenCount", 0)
        if cached > prompt:
            return None
        thoughts = counts.get("thoughtsTokenCount")
        total = meta.get("totalTokenCount")
        if total is not None:
            total = _count(total)
            if total is None:
                return None
        if thoughts is None:
            # Absent thoughts count as zero only when the total proves it.
            if total is None or total != prompt + tool_use + candidates:
                return None
            thoughts = 0
        elif total is not None and total != prompt + tool_use + candidates + thoughts:
            return None
        for key in ("promptTokensDetails", "candidatesTokensDetails"):
            for detail in meta.get(key) or ():
                if not isinstance(detail, dict):
                    return None
                modality = detail.get("modality")
                tokens = _count(detail.get("tokenCount", 0))
                if tokens is None:
                    return None
                if tokens and modality not in (None, "TEXT"):
                    return None  # image, audio or video tokens: other rates
        for candidate in response.get("candidates") or ():
            if isinstance(candidate, dict) and (
                candidate.get("groundingMetadata")
                or candidate.get("urlContextMetadata")
            ):
                return None  # tool fees the tariff doesn't price
        # Tool-use prompt tokens are billed as input.
        uncached = prompt + tool_use - cached
        return Usage(
            uncached=uncached,
            cached=cached,
            cache_write=0,
            output=candidates + thoughts,
        )
