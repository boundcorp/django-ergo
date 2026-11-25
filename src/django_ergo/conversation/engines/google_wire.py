"""The Gemini REST wire format: Ergo messages in, Ergo events out.

Pure functions (no network, no ORM), so the mapping is tested on its own.
The engine keeps its history and tool schemas in the OpenAI chat dialect
(``OpenAIAPIEngine``'s), and this module converts at the boundary:

- ``build_request`` turns messages, tool schemas and options into a
  ``generateContent`` body (system instruction, ``contents``, function
  declarations, generation and thinking config);
- ``parse_response`` turns a response (or the chunks of a stream, merged by
  ``merge_stream``) into a :class:`Reply`;
- ``raise_for_status`` maps HTTP errors to :mod:`google_errors`.

Reference: https://ai.google.dev/gemini-api/docs (generateContent, v1beta).
"""

from __future__ import annotations

import json
import os
import uuid
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field

from django_ergo.conversation.engines.google_errors import APIConnectionError
from django_ergo.conversation.engines.google_errors import AuthenticationError
from django_ergo.conversation.engines.google_errors import BadRequestError
from django_ergo.conversation.engines.google_errors import EmptyResponseError
from django_ergo.conversation.engines.google_errors import InternalServerError
from django_ergo.conversation.engines.google_errors import OverloadedError
from django_ergo.conversation.engines.google_errors import QuotaExhaustedError
from django_ergo.conversation.engines.google_errors import RateLimitError

BASE_URL = "https://generativelanguage.googleapis.com"
API_VERSION = "v1beta"
# The key is read from the first of these that is set. Never stored in config
# dicts, logs or error messages.
KEY_ENVS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
CALL_ID_PREFIX = "call_"
# Documented stand-in for a signature we don't have (history from another
# model, or stored without one). Gemini 3 rejects a function call without it.
DUMMY_SIGNATURE = "skip_thought_signature_validator"

# finishReason values that mean a safety system declined the content.
BLOCKED_FINISH = frozenset(
    {
        "SAFETY",
        "RECITATION",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
        "SPII",
        "IMAGE_SAFETY",
        "IMAGE_PROHIBITED_CONTENT",
        "IMAGE_RECITATION",
    }
)
# finishReason values for a function call Google couldn't make or accept.
BAD_CALL_FINISH = frozenset(
    {"MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL", "TOO_MANY_TOOL_CALLS"}
)

_EFFORTS = {
    "none": 0,
    "minimal": 1,
    "low": 2,
    "medium": 3,
    "high": 4,
    "xhigh": 5,
    "max": 5,
}
_LEVEL_NAMES = ("minimal", "low", "medium", "high")
# 2.5 thinking budgets per effort rank (1..5), and each model's allowed range.
_BUDGETS = {1: 512, 2: 1024, 3: 8192, 4: 24576, 5: 32768}


# -- credentials ----------------------------------------------------------------


def api_key(env_name: str = "") -> str:
    """The key from ``env_name`` (default: GEMINI_API_KEY, then GOOGLE_API_KEY)."""
    for name in dict.fromkeys((env_name, *KEY_ENVS)):
        if name and (value := os.environ.get(name, "").strip()):
            return value
    return ""


def configured(env_name: str = "") -> bool:
    """True when a key is set. Never reveals the key."""
    return bool(api_key(env_name))


def scrub(text: str, key: str) -> str:
    return text.replace(key, "[redacted]") if key and key in text else text


# -- schemas --------------------------------------------------------------------

_SCHEMA_KEYS = frozenset(
    {
        "type",
        "format",
        "description",
        "nullable",
        "enum",
        "items",
        "properties",
        "required",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "anyOf",
        "propertyOrdering",
        "title",
    }
)
_MAX_REF_DEPTH = 6


def clean_schema(schema, defs: dict | None = None, depth: int = 0):  # noqa: C901, PLR0912
    """A JSON Schema as the OpenAPI subset Gemini takes.

    ``$ref`` is inlined from ``$defs`` (to a bounded depth), ``type: [x, null]``
    becomes ``nullable``, ``const`` becomes a one-value ``enum``, and keywords
    Gemini rejects (``additionalProperties``, ``$schema``, ``default``,
    ``examples``, ...) are dropped.
    """
    if not isinstance(schema, dict):
        return {"type": "string"}
    defs = {**(defs or {}), **(schema.get("$defs") or {})}
    ref = schema.get("$ref")
    if isinstance(ref, str):
        target = defs.get(ref.rpartition("/")[2])
        if target is None or depth >= _MAX_REF_DEPTH:
            return {"type": "object"}
        return clean_schema(
            {**target, **{k: v for k, v in schema.items() if k != "$ref"}},
            defs,
            depth + 1,
        )
    out: dict = {}
    kind = schema.get("type")
    if isinstance(kind, list):
        kinds = [k for k in kind if k != "null"]
        if len(kinds) != len(kind):
            out["nullable"] = True
        kind = kinds[0] if kinds else "string"
    if kind:
        out["type"] = kind
    if "const" in schema and "enum" not in schema:
        out["enum"] = [schema["const"]]
    for key, value in schema.items():
        if key not in _SCHEMA_KEYS or key == "type" or key == "title":  # noqa: PLR1714
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {k: clean_schema(v, defs, depth) for k, v in value.items()}
        elif key == "items":
            out[key] = clean_schema(value, defs, depth)
        elif key == "anyOf" and isinstance(value, list):
            variants = [clean_schema(v, defs, depth) for v in value]
            plain = [v for v in variants if v != {"type": "null"}]
            if len(plain) == 1 and len(variants) == 2:  # noqa: PLR2004 -- Optional[X]
                out.update({**plain[0], "nullable": True})
            else:
                out[key] = plain
        elif key == "required" and isinstance(value, list):
            out[key] = [r for r in value if r in (schema.get("properties") or {})]
        else:
            out[key] = value
    if out.get("type") == "object" and "properties" not in out:
        out["properties"] = {}
    if not out:
        out["type"] = "object"
    if out.get("required") == []:
        del out["required"]
    return out


def function_declarations(tools: list[dict] | None) -> list[dict]:
    """OpenAI-style function tool schemas as Gemini ``functionDeclarations``."""
    declarations = []
    for tool in tools or []:
        fn = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        params = clean_schema(fn.get("parameters") or fn.get("input_schema") or {})
        declaration: dict = {
            "name": fn["name"],
            "description": fn.get("description") or fn["name"],
        }
        if params.get("properties"):
            declaration["parameters"] = params
        declarations.append(declaration)
    return declarations


# -- messages -> request ------------------------------------------------------------


def _data_url(url: str) -> tuple[str, str] | None:
    """(media type, base64 data) of a ``data:`` URL, else None."""
    if not url.startswith("data:") or ";base64," not in url:
        return None
    head, _, data = url[5:].partition(";base64,")
    return head or "application/octet-stream", data


def _content_parts(content) -> list[dict]:  # noqa: C901
    if content is None or content == "":
        return []
    if isinstance(content, str):
        return [{"text": content}]
    parts: list[dict] = []
    for block in content:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind == "text":
            if block.get("text"):
                parts.append({"text": block["text"]})
        elif kind == "image_url":
            url = (block.get("image_url") or {}).get("url", "")
            if inline := _data_url(url):
                parts.append({"inlineData": {"mimeType": inline[0], "data": inline[1]}})
            elif url:
                # Gemini fetches only Google-hosted files; say where it was.
                parts.append({"text": f"[image: {url}]"})
        elif kind == "file":
            file = block.get("file") or {}
            if inline := _data_url(file.get("file_data", "")):
                parts.append({"inlineData": {"mimeType": inline[0], "data": inline[1]}})
        elif kind == "input_audio":
            audio = block.get("input_audio") or {}
            fmt = audio.get("format") or "wav"
            parts.append(
                {
                    "inlineData": {
                        "mimeType": f"audio/{fmt}",
                        "data": audio.get("data", ""),
                    }
                }
            )
        elif isinstance(block, dict) and block.get("text"):
            parts.append({"text": str(block["text"])})
    return parts


def _args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _function_call(call: dict, *, signature_needed: bool, first: bool) -> dict:
    fn = call.get("function") or {}
    part: dict = {
        "functionCall": {"name": fn.get("name", ""), "args": _args(fn.get("arguments"))}
    }
    call_id = call.get("id") or ""
    if call_id and not call_id.startswith(CALL_ID_PREFIX):
        part["functionCall"]["id"] = call_id  # Google's own id: return it as it was
    signature = call.get("thought_signature")
    if signature:
        part["thoughtSignature"] = signature
    elif signature_needed and first:
        part["thoughtSignature"] = DUMMY_SIGNATURE
    return part


def _tool_response(message: dict, names: dict[str, str]) -> dict:
    call_id = message.get("tool_call_id") or ""
    content = message.get("content")
    if isinstance(content, list):
        content = "\n".join(
            str(b.get("text", "")) for b in content if isinstance(b, dict)
        )
    key = "error" if message.get("is_error") else "result"
    response: dict = {
        "name": names.get(call_id) or "tool",
        "response": {key: content or ""},
    }
    if call_id and not call_id.startswith(CALL_ID_PREFIX):
        response["id"] = call_id
    return {"functionResponse": response}


def to_contents(messages: list[dict], *, model: str = "") -> tuple[str, list[dict]]:  # noqa: C901
    """(system text, Gemini ``contents``) for OpenAI-dialect ``messages``.

    Consecutive same-role turns merge (all results of parallel calls arrive in
    one user turn, after the one model turn that made the calls).
    """
    system: list[str] = []
    contents: list[dict] = []
    names: dict[str, str] = {}
    signatures = model.removeprefix("models/").startswith("gemini-3")

    def add(role: str, parts: list[dict]) -> None:
        if not parts:
            return
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": list(parts)})

    for message in messages:
        role = message.get("role")
        if role in ("system", "developer"):
            text = message.get("content")
            if isinstance(text, list):
                text = "\n".join(str(b.get("text", "")) for b in text)
            if text:
                system.append(str(text))
        elif role == "assistant":
            parts = _content_parts(message.get("content"))
            calls = message.get("tool_calls") or []
            for call in calls:
                names[call.get("id", "")] = (call.get("function") or {}).get("name", "")
            parts += [
                _function_call(call, signature_needed=signatures, first=i == 0)
                for i, call in enumerate(calls)
            ]
            add("model", parts)
        elif role == "tool":
            add("user", [_tool_response(message, names)])
        else:
            add("user", _content_parts(message.get("content")))
    if contents and contents[0]["role"] != "user":
        contents.insert(
            0, {"role": "user", "parts": [{"text": "(conversation start)"}]}
        )
    return "\n\n".join(system), contents


# -- thinking -----------------------------------------------------------------------


def _allowed_levels(model: str) -> tuple[str, ...]:
    if "3-pro" in model and "3.1" not in model:
        return ("low", "high")
    if "3.1-pro" in model or "3.8" in model:
        return ("low", "medium", "high")
    return _LEVEL_NAMES


def _budget_range(model: str) -> tuple[int, int, bool]:
    """(min, max, can_disable) thinkingBudget for a 2.5 model."""
    if "pro" in model:
        return 128, 32768, False
    if "lite" in model:
        return 512, 24576, True
    return 0, 24576, True


def thinking_config(
    model: str,
    effort: str | None = None,
    *,
    level: str | None = None,
    budget: int | None = None,
    include_thoughts: bool = False,
) -> dict | None:
    """``generationConfig.thinkingConfig`` for ``model``, or None when the
    model doesn't take one.

    Gemini 3 takes a ``thinkingLevel``, 2.5 a ``thinkingBudget`` (never both).
    ``effort`` (none/minimal/low/medium/high/xhigh) maps to the nearest value
    the model accepts; ``level`` / ``budget`` set one directly. Models that
    can't turn thinking off get their smallest setting for ``none``.
    """
    name = model.removeprefix("models/")
    config: dict = {}
    rank = _EFFORTS.get(str(effort or "").lower())
    if name.startswith("gemini-3"):
        allowed = _allowed_levels(name)
        wanted = level or (
            None if rank is None else _LEVEL_NAMES[min(max(rank - 1, 0), 3)]
        )
        if wanted:
            order = _LEVEL_NAMES
            config["thinkingLevel"] = min(
                allowed, key=lambda a: abs(order.index(a) - order.index(wanted))
            )
    elif name.startswith("gemini-2.5"):
        low, high, can_disable = _budget_range(name)
        wanted_budget = budget
        if wanted_budget is None and rank is not None:
            wanted_budget = 0 if rank == 0 else _BUDGETS[rank]
        if wanted_budget is not None:
            if wanted_budget != -1:
                if wanted_budget == 0 and not can_disable:
                    wanted_budget = low
                config["thinkingBudget"] = min(max(wanted_budget, low), high)
            else:
                config["thinkingBudget"] = -1
    else:
        return None
    if include_thoughts:
        config["includeThoughts"] = True
    return config or None


# -- request ------------------------------------------------------------------------


def build_request(  # noqa: PLR0913
    messages: list[dict],
    *,
    model: str,
    tools: list[dict] | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    effort: str | None = None,
    thinking: dict | None = None,
    response_schema: dict | None = None,
    force_tool: str | None = None,
    system: str = "",
) -> dict:
    """A ``generateContent`` request body."""
    system_text, contents = to_contents(messages, model=model)
    system_text = "\n\n".join(t for t in (system, system_text) if t)
    body: dict = {"contents": contents}
    if system_text:
        body["systemInstruction"] = {"parts": [{"text": system_text}]}
    generation: dict = {}
    if max_tokens:
        generation["maxOutputTokens"] = int(max_tokens)
    if temperature is not None:
        generation["temperature"] = temperature
    thinking = thinking if thinking is not None else thinking_config(model, effort)
    if thinking:
        generation["thinkingConfig"] = thinking
    if response_schema is not None:
        generation["responseMimeType"] = "application/json"
        generation["responseSchema"] = clean_schema(response_schema)
    if generation:
        body["generationConfig"] = generation
    if declarations := function_declarations(tools):
        body["tools"] = [{"functionDeclarations": declarations}]
        config: dict = {"mode": "ANY" if force_tool else "AUTO"}
        if force_tool:
            config["allowedFunctionNames"] = [force_tool]
        body["toolConfig"] = {"functionCallingConfig": config}
    return body


# -- response -----------------------------------------------------------------------


@dataclass
class Reply:
    """One model response, in Ergo terms."""

    text: str = ""
    thoughts: str = ""
    calls: list[dict] = field(default_factory=list)
    finish_reason: str = "stop"  # stop, tool_calls, length or content_filter
    google_finish: str = ""
    refusal_category: str = ""
    usage: dict = field(default_factory=dict)
    model_version: str = ""


def usage_parts(metadata: dict | None) -> dict:
    """Token usage split the way it's billed.

    Gemini's ``promptTokenCount`` includes ``cachedContentTokenCount``; tool-use
    prompt tokens are billed as input; ``candidatesTokenCount`` leaves out
    ``thoughtsTokenCount``, which is billed as output. So ``input_tokens`` is
    the uncached rest, ``cache_read_input_tokens`` the cached part,
    ``output_tokens`` candidates plus thoughts, ``reasoning_tokens`` the
    thoughts. All None without usage metadata.
    """
    if not isinstance(metadata, dict):
        return dict.fromkeys(
            (
                "input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "output_tokens",
                "reasoning_tokens",
            )
        )

    def count(name: str) -> int:
        value = metadata.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    cached = count("cachedContentTokenCount")
    prompt = count("promptTokenCount") + count("toolUsePromptTokenCount")
    thoughts = count("thoughtsTokenCount")
    return {
        "input_tokens": max(0, prompt - cached),
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": 0,  # implicit caching has no write charge
        "output_tokens": count("candidatesTokenCount") + thoughts,
        "reasoning_tokens": thoughts,
    }


def new_call_id() -> str:
    return f"{CALL_ID_PREFIX}{uuid.uuid4().hex[:24]}"


def parse_response(data: dict) -> Reply:  # noqa: C901
    """A ``generateContent`` response as a :class:`Reply`.

    Raises a typed error when Google returned nothing usable (no candidate and
    no block reason, or a malformed function call). A safety block is not an
    error here: it comes back as ``finish_reason == "content_filter"`` so the
    runner can offer the turn on another model.
    """
    reply = Reply(
        usage=usage_parts(data.get("usageMetadata")),
        model_version=str(data.get("modelVersion") or ""),
    )
    candidates = data.get("candidates") or []
    block = (data.get("promptFeedback") or {}).get("blockReason")
    if not candidates:
        if block:
            reply.finish_reason = "content_filter"
            reply.google_finish = reply.refusal_category = str(block)
            return reply
        msg = "Gemini returned no candidates (500-class empty response)"
        raise EmptyResponseError(msg, status=200, reason="EMPTY")
    candidate = candidates[0]
    texts, thoughts = [], []
    for part in (candidate.get("content") or {}).get("parts") or []:
        if "functionCall" in part:
            call = part["functionCall"]
            args = call.get("args")
            reply.calls.append(
                {
                    "id": call.get("id") or new_call_id(),
                    "name": call.get("name", ""),
                    "input": args if isinstance(args, dict) else {},
                    "argument_chars": len(json.dumps(args or {})),
                    **(
                        {"thought_signature": part["thoughtSignature"]}
                        if part.get("thoughtSignature")
                        else {}
                    ),
                }
            )
        elif part.get("thought"):
            thoughts.append(part.get("text") or "")
        elif part.get("text"):
            texts.append(part["text"])
    reply.text = "".join(texts)
    reply.thoughts = "".join(thoughts)
    finish = str(candidate.get("finishReason") or "")
    reply.google_finish = finish
    if finish in BAD_CALL_FINISH:
        detail = candidate.get("finishMessage") or ""
        msg = f"Gemini couldn't complete the function call ({finish}) {detail}".strip()
        raise InternalServerError(msg, status=200, reason=finish)
    if finish in BLOCKED_FINISH:
        reply.finish_reason = "content_filter"
        reply.refusal_category = finish
    elif finish == "MAX_TOKENS":
        reply.finish_reason = "length"
    elif reply.calls:
        reply.finish_reason = "tool_calls"
    if not (reply.text or reply.calls or reply.thoughts) and finish in ("OTHER", ""):
        msg = f"Gemini returned an empty response (finishReason {finish or 'missing'})"
        raise EmptyResponseError(msg, status=200, reason=finish or "EMPTY")
    return reply


def merge_stream(chunks: list[dict]) -> dict:  # noqa: C901
    """The chunks of a ``streamGenerateContent`` stream as one response.

    Adjacent text parts of the same kind join (parts that carry a signature
    stay whole), the last ``usageMetadata`` and ``finishReason`` win.
    """
    merged: dict = {}
    parts: list[dict] = []
    for chunk in chunks:
        for key in ("usageMetadata", "modelVersion", "promptFeedback"):
            if key in chunk:
                merged[key] = chunk[key]
        for candidate in chunk.get("candidates") or []:
            slot = merged.setdefault("candidates", [{}])[0]
            for key in ("finishReason", "finishMessage", "safetyRatings"):
                if key in candidate:
                    slot[key] = candidate[key]
            for part in (candidate.get("content") or {}).get("parts") or []:
                last = parts[-1] if parts else None
                joinable = (
                    last is not None
                    and "text" in part
                    and "text" in last
                    and bool(part.get("thought")) == bool(last.get("thought"))
                    and not last.get("thoughtSignature")
                )
                if joinable:
                    last["text"] += part["text"]
                    if part.get("thoughtSignature"):
                        last["thoughtSignature"] = part["thoughtSignature"]
                else:
                    parts.append(deepcopy(part))
    if merged.get("candidates"):
        merged["candidates"][0]["content"] = {"role": "model", "parts": parts}
    return merged


# -- errors -------------------------------------------------------------------------

_EXHAUSTED_HINTS = (
    "perday",
    "per day",
    "daily",
    "billing",
    "spend",
    "exceeded your current quota",
)


def _retry_delay(details: list) -> float | None:
    for detail in details or []:
        delay = detail.get("retryDelay") if isinstance(detail, dict) else None
        if isinstance(delay, str) and delay.endswith("s"):
            try:
                return float(delay[:-1])
            except ValueError:
                return None
    return None


def _quota_ids(details: list) -> str:
    ids = []
    for detail in details or []:
        for violation in (
            (detail.get("violations") or []) if isinstance(detail, dict) else []
        ):
            ids.append(str(violation.get("quotaId", "")))  # noqa: PERF401
    return " ".join(ids)


def error_for(status: int, body, *, key: str = "", retry_header: str = ""):  # noqa: PLR0911
    """The typed exception for a failed response (``body`` is its decoded JSON, or text)."""
    error = body.get("error") if isinstance(body, dict) else None
    error = error if isinstance(error, dict) else {}
    message = scrub(str(error.get("message") or body or "request failed")[:600], key)
    reason = str(error.get("status") or "")
    details = error.get("details") or []
    retry = _retry_delay(details)
    if retry is None and retry_header:
        try:
            retry = float(retry_header)
        except ValueError:
            retry = None
    label = f"Gemini {status} {reason}".strip()
    kwargs = {"status": status, "reason": reason, "retry_after": retry}
    invalid_key = "api key not valid" in message.lower() or any(
        isinstance(d, dict) and d.get("reason") == "API_KEY_INVALID" for d in details
    )
    if status in (401, 403) or invalid_key or reason == "UNAUTHENTICATED":
        return AuthenticationError(f"{label}: {message}", **kwargs)
    if status == 429 or reason == "RESOURCE_EXHAUSTED":  # noqa: PLR2004
        haystack = f"{_quota_ids(details)} {message}".lower()
        if any(hint in haystack for hint in _EXHAUSTED_HINTS):
            return QuotaExhaustedError(
                f"Gemini quota exhausted, rate limit 429 {reason}: {message}", **kwargs
            )
        return RateLimitError(f"Gemini rate limit 429 {reason}: {message}", **kwargs)
    if status == 503 or reason == "UNAVAILABLE":  # noqa: PLR2004
        return OverloadedError(f"{label}: {message}", **kwargs)
    if status in (400, 404, 413, 422) or reason in ("INVALID_ARGUMENT", "NOT_FOUND"):
        return BadRequestError(f"{label}: {message}", **kwargs)
    if status >= 500 or reason in ("INTERNAL", "DEADLINE_EXCEEDED"):  # noqa: PLR2004
        return InternalServerError(f"{label}: {message}", **kwargs)
    return BadRequestError(f"{label}: {message}", **kwargs)


def connection_error(exc: Exception, key: str = "") -> APIConnectionError:
    return APIConnectionError(
        scrub(f"Couldn't reach Gemini: {type(exc).__name__}", key), reason="CONNECTION"
    )
