"""Scoped Orca admission and fixed, read-only execution-host checks.

This file also runs standalone over Orca's files_host SSH route. It is not
an arbitrary command runner, nor an agent shell sandbox.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path


def host_check(root: str = "") -> dict:
    if root:

        def git(*args):
            return subprocess.check_output(  # noqa: S603 — fixed git operations
                ["git", "-C", root, *args],  # noqa: S607 — fixed git binary
                text=True,
            ).strip()

        return {
            "clean": not git("status", "--porcelain", "--untracked-files=all"),
            "head": git("rev-parse", "HEAD"),
            "base": git("rev-parse", "origin/main"),
        }
    directory = Path(
        os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".omp/agent"
    )
    database = directory / "models.db"
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        rows = connection.execute("SELECT provider_id, models FROM model_cache")
        catalogs = {provider: json.loads(models) for provider, models in rows}
    with sqlite3.connect(
        f"{(directory / 'agent.db').as_uri()}?mode=ro", uri=True
    ) as connection:
        credentials = list(
            connection.execute(
                "SELECT DISTINCT provider, credential_type FROM auth_credentials "
                "WHERE disabled_cause IS NULL"
            )
        )
    oauth = {provider for provider, kind in credentials if kind == "oauth"}
    other = {provider for provider, kind in credentials if kind != "oauth"}
    # Fail closed rather than let the harness choose a paid credential.
    for provider, key in (
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("openai", "OPENAI_API_KEY"),
        ("openai-codex", "OPENAI_API_KEY"),
        ("xai-oauth", "XAI_API_KEY"),
    ):
        if os.environ.get(key):
            other.add(provider)
    return {
        "omp": bool(shutil.which("omp")),
        "catalogs": catalogs,
        "subscriptions": sorted(oauth - other),
    }


def _chat_cache_record(raw: dict) -> dict:
    """Add explicit chat evidence for omp cache rows without a `kind` field."""
    max_tokens = raw.get("maxTokens")
    if (
        "kind" not in raw
        and isinstance(raw.get("api"), str)
        and raw["api"]
        in {"anthropic-messages", "openai-codex-responses", "openai-responses"}
        and isinstance(raw.get("input"), list)
        and "text" in raw["input"]
        and isinstance(max_tokens, int | float)
        and max_tokens > 0
    ):
        return {**raw, "kind": "chat"}
    return raw


def admitted_providers(providers, metadata: dict):  # noqa: C901, PLR0912 — one admission/filtering pass
    """A filtered copy: listed, priced chat models on coding subscriptions only.

    Reuse discovery's eligibility rules; never fetch models, compile routing,
    modify configuration, or fall back to an API account here.
    """
    from django_ergo.bots.model_catalog import ALLOWED_FOR_CODING
    from django_ergo.bots.model_catalog import Source
    from django_ergo.bots.model_catalog import _normalize

    if not metadata.get("omp"):
        msg = "Scoped Orca runner requires omp on the execution host"
        raise ValueError(msg)
    allowed = {}
    native_agents = {"anthropic": "claude", "openai-codex": "codex"}
    for alias, provider in providers.providers.items():
        canonical = (
            provider.account
            if provider.worker_only
            else "anthropic"
            if provider.type == "claude"
            else "openai-codex"
            if provider.type == "openai"
            else provider.type
        )
        if (
            canonical not in ALLOWED_FOR_CODING
            or canonical not in metadata.get("subscriptions", [])
            or provider.transport != "cli"
        ):
            continue
        source = Source(
            alias,
            canonical,
            subscription=True,
            custom_endpoint=any(
                provider.config.get(key)
                for key in ("base_url", "api_base", "endpoint", "url")
            ),
        )
        for raw in metadata.get("catalogs", {}).get(canonical, []):
            if raw.get("enabled") is False:
                continue
            record = _normalize(
                source, _chat_cache_record(raw), "omp_cache", providers, ()
            )
            if record and record["routable"]:
                # A provider's configured chat list is an additional allowlist.
                if not provider.worker_only and record["id"] not in provider.models:
                    continue
                configured = provider.models.get(record["id"])
                if configured and (
                    configured.config.get("enabled") is False
                    or configured.config.get("disabled")
                ):
                    continue
                allowed[(alias, record["id"])] = f"{canonical}/{record['id']}"
    tiers = {}
    for tier, choices in providers.routing.agents.items():
        tiers[tier] = []
        for choice in choices:
            namespace, separator, model = choice.model.partition("/")
            if not separator:
                model = namespace
            selector = allowed.get((choice.provider, model))
            valid_namespace = not separator or namespace in {
                choice.provider,
                (selector or "").partition("/")[0],
            }
            if not selector or not valid_namespace:
                continue
            expected_agent = native_agents.get(selector.partition("/")[0], "omp")
            if choice.agent == expected_agent:
                # Orca's native CLIs take bare model IDs; omp takes its namespace.
                tiers[tier].append(
                    replace(
                        choice, model=model if expected_agent != "omp" else selector
                    )
                )
    return replace(providers, routing=replace(providers.routing, agents=tiers)), set(
        allowed.values()
    )


if __name__ == "__main__":
    print(json.dumps(host_check(sys.argv[1] if len(sys.argv) > 1 else "")))  # noqa: T201
