"""Non-billable model discovery. Catalog membership never changes routing.

Only local metadata and provider model-list GET endpoints are consulted. In
particular, no engine, inference request, CLI turn, or routing compiler runs.
The local omp cache is read-only; its credential registry is queried for
provider names/types only, never credential payloads.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx
import yaml
from django.db import transaction

from django_ergo.conversation.inference_policy import names_private_model

ALLOWED_FOR_CODING = frozenset({"anthropic", "openai", "openai-codex", "xai-oauth"})
KEY_ENVS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "xai": "XAI_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "google": "GEMINI_API_KEY",
}
PRICING_URLS = {
    "anthropic": "https://platform.claude.com/docs/en/about-claude/pricing",
    "openai": "https://openai.com/api/pricing/",
    "openai-codex": "https://openai.com/api/pricing/",
    "xai": "https://docs.x.ai/developers/models",
    "xai-oauth": "https://docs.x.ai/developers/models",
    "fireworks": "https://fireworks.ai/pricing",
    "google": "https://ai.google.dev/gemini-api/docs/pricing",
}
ENDPOINTS = {
    "anthropic": "https://api.anthropic.com/v1/models",
    "openai": "https://api.openai.com/v1/models",
    "xai": "https://api.x.ai/v1/models",
    "fireworks": "https://api.fireworks.ai/inference/v1/models",
    "google": "https://generativelanguage.googleapis.com/v1beta/models",
}
CAPABILITIES = (
    "reasoning",
    "contextWindow",
    "maxTokens",
    "input",
    "thinking",
    "kind",
    "supportsTools",
    "supportsComputerUse",
    "context_length",
    "capabilities",
    "inputTokenLimit",
    "outputTokenLimit",
    "supportedGenerationMethods",
    "contextLength",
    "supportsImageInput",
    "public",
    "max_input_tokens",
    "max_tokens",
    "lifecycle",
    "deprecated_at",
    "retires_at",
    "line",
)
PRICE_KEYS = {"input", "output", "cacheRead", "cacheWrite", "longContext"}
MIN_SECRET_LENGTH = 6
NON_CHAT_MODEL_ID = re.compile(
    r"(?:^|[-._/])(?:tts|stt|transcribe|transcription|embedding|embeddings|"
    r"realtime|moderation|image|video|audio)(?:$|[-._/])",
    re.IGNORECASE,
)


class ModelCatalogError(RuntimeError):
    """A catalog could not be read; no snapshot was advanced.

    Upstream exception text, response bodies, URLs, headers and credentials
    deliberately never appear in this exception or its displayed traceback.
    """

    def __init__(self, provider: str, source: str):
        self.provider = (
            provider
            if provider in PRICING_URLS.keys() | {"local", "other"}
            else "other"
        )
        self.source = (
            source
            if source
            in {"omp_cache", "omp_config", "omp_configuration", "official_list"}
            else "catalog"
        )
        super().__init__(f"Model catalog fetch failed ({self.provider}, {self.source})")


@dataclass
class Source:
    alias: str
    provider: str
    key_env: str = ""
    private: bool = False
    custom_endpoint: bool = False
    subscription: bool = False


def _agent_dir() -> Path:
    return Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".omp/agent")


def _local_configuration() -> set[str]:
    """Configured omp providers, without reading any saved credential value."""
    root = _agent_dir()
    result = set()
    config = root / "config.yml"
    if config.exists():
        try:
            data = yaml.safe_load(config.read_text()) or {}
            for name, spec in (data.get("providers") or {}).items():
                if not isinstance(spec, dict) or spec.get("enabled") is not False:
                    result.add(str(name))
        except Exception:  # noqa: BLE001 — configuration errors must not expose secrets
            provider = "local"
            raise ModelCatalogError(provider, "omp_config") from None
    database = root / "agent.db"
    if database.exists():
        try:
            with sqlite3.connect(
                f"{database.as_uri()}?mode=ro", uri=True
            ) as connection:
                result.update(
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT provider FROM auth_credentials "
                        "WHERE disabled_cause IS NULL"
                    )
                )
        except Exception:  # noqa: BLE001 — credential registry errors must not expose secrets
            provider = "local"
            raise ModelCatalogError(provider, "omp_configuration") from None
    # Standard credential environment variables are themselves configuration.
    result.update(p for p, key in KEY_ENVS.items() if os.environ.get(key))
    if os.environ.get("GOOGLE_API_KEY"):
        result.add("google")
    return result


def _sources(providers) -> list[Source]:
    sources = []
    for alias, provider in providers.providers.items():
        canonical = "anthropic" if provider.type == "claude" else provider.type
        if provider.transport == "cli" and canonical == "openai":
            canonical = "openai-codex"
        sources.append(
            Source(
                alias,
                canonical,
                provider.api_key_env,
                custom_endpoint=any(
                    provider.config.get(k)
                    for k in ("base_url", "api_base", "endpoint", "url")
                ),
                subscription=provider.transport == "cli",
            )
        )
    sources.extend(
        Source(name, pool.provider, KEY_ENVS.get(pool.provider, ""), private=True)
        for name, pool in providers.private_pools.items()
    )
    present = {s.provider for s in sources}
    sources.extend(
        Source(
            provider,
            provider,
            KEY_ENVS.get(provider, ""),
            private=provider == "fireworks",
        )
        for provider in sorted(_local_configuration() - present)
    )
    return sources


def _local_catalog(provider: str) -> tuple[list[dict] | None, dict]:
    database = _agent_dir() / "models.db"
    if not database.exists():
        return None, {
            "source": "omp_cache",
            "status": "unhandled",
            "reason": "local catalog missing",
        }
    try:
        with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
            row = connection.execute(
                "SELECT models, authoritative FROM model_cache WHERE provider_id = ?",
                (provider,),
            ).fetchone()
        if row is None:
            return None, {
                "source": "omp_cache",
                "status": "unhandled",
                "reason": "provider absent from local catalog",
            }
        models = json.loads(row[0])
        if not isinstance(models, list) or not all(isinstance(m, dict) for m in models):
            raise ValueError  # noqa: TRY301 — invalid cache data uses the sanitized failure boundary
        return models, {
            "source": "omp_cache",
            "status": "ok",
            "authoritative_membership": bool(row[1]),
            "count": len(models),
        }
    except Exception:  # noqa: BLE001 — cache errors must not expose credentials or payloads
        raise ModelCatalogError(
            provider if provider in PRICING_URLS else "other", "omp_cache"
        ) from None


def _official_models(source: Source) -> list[dict] | None:  # noqa: C901, PLR0912 — keep fixed-endpoint pagination in one failure boundary
    """List all pages at fixed official endpoints; never follow server URLs."""
    provider = source.provider
    key = os.environ.get(source.key_env or KEY_ENVS.get(provider, ""))
    if provider == "google" and not key:
        key = os.environ.get("GOOGLE_API_KEY")
    if (
        source.subscription
        or not key
        or provider not in ENDPOINTS
        or source.custom_endpoint
    ):
        return None
    endpoint = ENDPOINTS[provider]
    if provider == "fireworks":
        account = os.environ.get("FIREWORKS_ACCOUNT_ID")
        if not account:
            return None
        endpoint = (
            f"https://api.fireworks.ai/v1/accounts/{quote(account, safe='')}/models"
        )
    headers = {"Authorization": f"Bearer {key}"}
    if provider == "anthropic":
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    elif provider == "google":
        headers = {"x-goog-api-key": key}
    models = []
    params = {}
    seen = set()
    try:
        with httpx.Client(
            timeout=30, follow_redirects=False, trust_env=False
        ) as client:
            while True:
                response = client.get(endpoint, headers=headers, params=params)
                response.raise_for_status()
                data = response.json()
                items = data.get(
                    "models" if provider in {"google", "fireworks"} else "data"
                )
                if not isinstance(items, list) or not all(
                    isinstance(m, dict) for m in items
                ):
                    raise ValueError  # noqa: TRY301 — invalid payloads use the sanitized failure boundary
                models.extend(items)
                token = data.get("nextPageToken")
                parameter = "pageToken"
                if provider == "anthropic" and data.get("has_more"):
                    token = data.get("last_id")
                    parameter = "after_id"
                    if not token:
                        raise ValueError  # noqa: TRY301 — invalid pagination uses the sanitized failure boundary
                elif data.get("has_more"):
                    # Unknown pagination must never imply removals.
                    raise ValueError  # noqa: TRY301 — invalid pagination uses the sanitized failure boundary
                if data.get("next") or data.get("next_page"):
                    raise ValueError  # noqa: TRY301 — untrusted pagination URLs must not be followed
                if not token:
                    break
                if not isinstance(token, str) or token in seen:
                    raise ValueError  # noqa: TRY301 — invalid pagination uses the sanitized failure boundary
                seen.add(token)
                params = {parameter: token}
        return models
    except Exception:  # noqa: BLE001 — HTTP and parsing errors must not expose credentials or payloads
        raise ModelCatalogError(provider, "official_list") from None


def _number(value) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _safe(value, secrets: tuple[str, ...]):
    """Keep JSON metadata only, stripping secret-shaped keys and known values."""
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, bool | int) or value is None:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, list):
        return [_safe(v, secrets) for v in value]
    if isinstance(value, dict):
        return {
            str(k): _safe(v, secrets)
            for k, v in value.items()
            if not any(
                word in str(k).lower()
                for word in ("key", "secret", "auth", "header", "url")
            )
            and str(k).lower() not in {"token", "access_token", "refresh_token"}
        }
    return None


def _normalize(
    source: Source, raw: dict, origin: str, providers, secrets
) -> dict | None:
    model_id = raw.get("id") or raw.get("name")
    if not isinstance(model_id, str) or not model_id:
        raise ModelCatalogError(
            source.provider if source.provider in PRICING_URLS else "other", origin
        )
    if source.provider == "google":
        model_id = model_id.removeprefix("models/")
    model_id = _safe(model_id, secrets)
    cost = raw.get("cost") or raw.get("pricing") or {}
    cost = (
        {k: v for k, v in cost.items() if k in PRICE_KEYS}
        if isinstance(cost, dict)
        else {}
    )
    if source.provider == "xai" and origin == "official_list":
        for upstream, local in (
            ("prompt_text_token_price", "input"),
            ("completion_text_token_price", "output"),
            ("cached_prompt_text_token_price", "cacheRead"),
        ):
            if _number(raw.get(upstream)):
                cost[local] = raw[upstream] / 10_000
    status = raw.get("pricingStatus")
    if (
        source.provider == "xai"
        and origin == "official_list"
        and all(
            _number(raw.get(k))
            for k in ("prompt_text_token_price", "completion_text_token_price")
        )
    ):
        status = "priced"
    priced = all(_number(cost.get(k)) for k in ("input", "output"))
    # Zero-filled catalog defaults do not establish that a model is free.
    priced = priced and (
        bool(cost.get("input") or cost.get("output"))
        or status in {"known", "priced", "fixed", "free"}
    )
    if status in {"unknown", "missing", "unpriced"}:
        priced = False
    private = (
        source.private
        or source.provider == "fireworks"
        or names_private_model(model_id)
        or providers.is_private(model_id)
    )
    quarantined = bool(
        raw.get("quarantined")
        or raw.get("quarantine")
        or raw.get("disabled")
        or source.custom_endpoint
        or raw.get("lifecycle") == "retired"
    )
    # Text input, generation limits and tools can also describe realtime or
    # other non-chat endpoints. Only explicit chat metadata establishes coding
    # eligibility; missing kind must not silently become an execution candidate.
    allowed = (
        source.provider in ALLOWED_FOR_CODING
        and raw.get("kind") == "chat"
        and not NON_CHAT_MODEL_ID.search(model_id)
        and raw.get("supportsTools") is not False
    )
    coding = allowed and priced and not private and not quarantined
    selector = f"{source.alias}/{model_id}"
    return {
        "id": model_id,
        "selector": selector,
        "provider": source.provider,
        "configured_provider": source.alias,
        "private": private,
        "quarantined": quarantined,
        "priced": priced,
        "allowed_for_coding": allowed,
        "routable": coding,
        "pricing": _safe(cost, secrets),
        "pricing_source": origin if cost else "unknown",
        "pricing_status": _safe(
            str(status or ("priced" if priced else "unknown")), secrets
        ),
        "pricing_authority": "provider"
        if origin == "official_list" and cost
        else "catalog"
        if cost
        else "unknown",
        "pricing_url": PRICING_URLS.get(source.provider, ""),
        "capabilities": _safe({k: raw[k] for k in CAPABILITIES if k in raw}, secrets),
        "provenance": {"source": origin, "provider": source.provider},
    }


def _routed(providers, bot) -> set[str]:
    refs = {providers.default} if providers.default else set()
    for models in providers.routing.tiers.values():
        refs.update(models)
    for choices in providers.routing.agents.values():
        for choice in choices:
            namespace, separator, model = choice.model.partition("/")
            # Agent configs may use a canonical omp selector but an Ergo alias
            # for its subscription (anthropic/model + provider: subscription).
            if (
                not separator
                or namespace not in PRICING_URLS.keys() | providers.providers.keys()
            ):
                model = choice.model
            refs.add(f"{choice.provider}/{model}")
    model_ref = getattr(bot, "model_ref", None)
    if callable(model_ref):
        pinned = model_ref()
        if pinned:
            refs.add(pinned)
            for pool in providers.private_pools.values():
                for alias, model in pool.models.items():
                    if pinned == f"{pool.name}/{alias}" or (
                        pinned == pool.name and alias == pool.default
                    ):
                        refs.add(f"{pool.name}/{model}")
    return refs


def check_available(context: dict) -> dict:  # noqa: C901, PLR0912, PLR0915 — discovery and its atomic snapshot diff form one operation
    """Discover configured providers and atomically diff their last good snapshot.

    Returns JSON-only diffs, policy flags, source coverage and a concise summary.
    A newly created baseline reports its count once, with empty diffs/flags and
    notify=True; existing baselines report only real changes, never reseeding
    merely because their model map is empty.
    Unhandled providers retain their previous baseline; a fetch/parse failure
    raises ModelCatalogError and advances no part of the snapshot.
    """
    from django_ergo.bots.schedule_workflow import safe_json
    from django_ergo.conversation.models import ModelCatalogSnapshot

    bot = context["bot"]
    providers = bot.providers
    sources = _sources(providers)
    secrets = tuple(
        value
        for name, value in os.environ.items()
        if len(value) >= MIN_SECRET_LENGTH
        and any(word in name.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
    )
    secrets += tuple(
        str(p.config["api_key"])
        for p in providers.providers.values()
        if len(str(p.config.get("api_key") or "")) >= MIN_SECRET_LENGTH
    )
    records = {}
    configured = {source.provider for source in sources}
    coverage = [
        {
            "provider": provider,
            "source": "configuration",
            "status": "not_configured",
            "reason": "no configured registry provider, local omp provider, or credential environment",
        }
        for provider in sorted(PRICING_URLS.keys() - configured)
    ]
    successful = set()
    cached = {}
    official = {}
    for source in sources:
        canonical = source.provider
        if canonical not in cached:
            cached[canonical] = _local_catalog(canonical)
        local, local_coverage = cached[canonical]
        coverage.append(
            {
                "provider": canonical,
                "configured_provider": source.alias,
                **local_coverage,
            }
        )
        official_key = (
            canonical,
            source.key_env,
            source.custom_endpoint,
            source.subscription,
        )
        if official_key not in official:
            official[official_key] = _official_models(source)
        remote = official[official_key]
        reason = "no supported official endpoint with configured credentials"
        if source.subscription:
            reason = "subscription membership uses the local catalog, not an API account list"
        elif source.custom_endpoint:
            reason = "custom endpoint is not an official provider"
        elif canonical == "fireworks" and not os.environ.get("FIREWORKS_ACCOUNT_ID"):
            reason = "Fireworks management list requires configured account ID"
        coverage.append(
            {
                "provider": canonical,
                "configured_provider": source.alias,
                "source": "official_list",
                "status": "ok" if remote is not None else "unhandled",
                **(
                    {"count": len(remote)} if remote is not None else {"reason": reason}
                ),
            }
        )
        if local is None and remote is None:
            continue
        successful.add(source.alias)
        metadata = {str(m.get("id") or m.get("name")): m for m in local or []}
        # Official lists establish membership when present; cache enriches exact IDs only.
        for raw in remote if remote is not None else local:
            model_id = raw.get("id") or raw.get("name")
            if remote is not None:
                model_id = (
                    str(model_id).removeprefix("models/")
                    if canonical == "google"
                    else model_id
                )
                merged = {**metadata.get(model_id, {}), **raw, "id": model_id}
                origin = "official_list"
            else:
                merged, origin = raw, "omp_cache"
            try:
                record = _normalize(source, merged, origin, providers, secrets)
            except ModelCatalogError:
                raise
            except Exception:  # noqa: BLE001 — normalization errors must not expose raw metadata or secrets
                raise ModelCatalogError(
                    canonical if canonical in PRICING_URLS else "other", origin
                ) from None
            if record is not None:
                if remote is not None and not any(
                    k in raw for k in ("cost", "pricing", "prompt_text_token_price")
                ):
                    record["pricing_source"] = (
                        "omp_cache" if record["pricing"] else "unknown"
                    )
                    record["pricing_authority"] = (
                        "catalog" if record["pricing"] else "unknown"
                    )
                records[record["selector"]] = record
    run = context.get("run")
    user = context.get("user")
    definition = getattr(bot, "definition", None)
    root = getattr(definition, "root_dir", None) or getattr(bot, "path", "")
    scope_data = [
        str(root),
        bot.name,
        str(getattr(user, "pk", "")),
        str(getattr(run, "schedule", "")),
    ]
    scope = hashlib.sha256(json.dumps(scope_data).encode()).hexdigest()
    with transaction.atomic():
        snapshot, baseline_seeded = ModelCatalogSnapshot.objects.get_or_create(
            scope=scope
        )
        snapshot = ModelCatalogSnapshot.objects.select_for_update().get(pk=snapshot.pk)
        previous = snapshot.models
        # Unavailable sources are not evidence of removal; preserve their last good data.
        current = {
            key: value
            for key, value in previous.items()
            if value["configured_provider"] not in successful
        }
        current.update(records)
        new = (
            []
            if baseline_seeded
            else [current[k] for k in sorted(current.keys() - previous.keys())]
        )
        removed = [previous[k] for k in sorted(previous.keys() - current.keys())]
        changed = [
            {"before": previous[k], "after": current[k]}
            for k in sorted(current.keys() & previous.keys())
            if current[k] != previous[k]
        ]
        routed = _routed(providers, bot)
        flags = [
            {
                "reason": "removed_in_route",
                "severity": "critical",
                "selector": record["selector"],
            }
            for record in removed
            if record["selector"] in routed
        ]
        noteworthy = new + [item["after"] for item in changed]
        for record in noteworthy:
            for reason, applies in (
                ("private", record["private"]),
                ("quarantined", record["quarantined"]),
                ("unpriced", not record["priced"]),
                (
                    "coding_provider_not_allowed",
                    record["provider"] not in ALLOWED_FOR_CODING,
                ),
                (
                    "coding_capability_not_supported",
                    record["provider"] in ALLOWED_FOR_CODING
                    and not record["allowed_for_coding"],
                ),
            ):
                if applies:
                    flags.append(
                        {
                            "reason": reason,
                            "severity": "warning",
                            "selector": record["selector"],
                        }
                    )
            if record["capabilities"].get("lifecycle") in {"deprecated", "retired"}:
                flags.append(
                    {
                        "reason": "model_" + record["capabilities"]["lifecycle"],
                        "severity": "warning",
                        "selector": record["selector"],
                    }
                )
        # Validate the exact workflow payload before committing its baseline.
        # Flags reference records already present in the diff, rather than
        # multiplying full pricing/capability metadata for each policy warning.
        report = safe_json(
            {
                "new": new,
                "removed": removed,
                "changed": changed,
                "flagged": flags,
                "baseline_seeded": baseline_seeded,
                "model_count": len(current),
                "summary": (
                    f"Baseline seeded, {len(current)} models"
                    if baseline_seeded
                    else (
                        f"{len(new)} new, {len(removed)} removed, "
                        f"{len(changed)} changed models"
                    )
                ),
                "notify": baseline_seeded or bool(new or removed or changed or flags),
                "coverage": coverage,
            }
        )
        snapshot.models = current
        snapshot.coverage = coverage
        snapshot.save(update_fields=["models", "coverage", "updated_at"])
    return report
