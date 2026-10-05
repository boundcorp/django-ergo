"""Model routing: pick a model by tier (low, medium, high) from what's left
on each subscription.

``providers.yaml`` lists each tier's candidates in order of preference, for
bot chats (``tiers``) and for coding agents started through Orca
(``agents``)::

    tiers:
      low:    [subscription/claude-sonnet-5-5, chatgpt/gpt-6-luna]
      medium: [subscription/claude-opus-5-5, chatgpt/gpt-6-sol]
      high:   [subscription/claude-opus-5-5, chatgpt/gpt-6-sol, openai/gpt-6-sol]
    agents:                      # subscriptions only: a candidate names its provider
      medium:
        - {agent: claude, model: claude-opus-5-5, provider: subscription}
        - {agent: codex, model: gpt-6-sol, effort: medium, provider: chatgpt}
    routing:                     # optional rules; routing.md can say it in words
      limits:
        - {provider: subscription, window: five_hour, max_used: 85}
        - {provider: chatgpt, window: weekly, max_used: 80}

A chat (or a bot's ``engine.model``) set to ``auto/<tier>`` gets a model per
turn from :func:`pick_model`: the first candidate whose provider is
available and under every limit. A chat keeps the model it had while that
model still qualifies, so a prompt cache isn't thrown away for nothing.
When every candidate is over a limit, the one whose subscription has the
most room wins. Agent candidates must be CLI subscriptions, never API keys.

Limits read :class:`ProviderUsage`: the latest 5-hour and weekly windows
each CLI engine reports (Claude Code's ``rate_limit_event``, Codex's
``account/rateLimits``). A provider that refused a call for its limit counts
as fully used until the window resets.

``routing.md`` next to ``providers.yaml`` states the priorities in plain
words ("lean on Claude until its 5-hour window is 85% used, keep Codex's
weekly window under 80%"). :func:`compile_routing` turns it into the same
``limits`` once per change of the file (a structured call), stored in
:class:`RoutingPolicy`; until then the YAML ``routing`` rules apply. Without
any rules, a provider is skipped only at 98% used. A deployment can replace
the policy entirely with ``DJANGO_ERGO["MODEL_ROUTER"]``, a dotted path to a
callable ``(candidates, usage, rules, current) -> candidate``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from pydantic import BaseModel
from pydantic import Field

if TYPE_CHECKING:
    from django_ergo.bots.providers import Providers

logger = logging.getLogger(__name__)

TIERS = ("low", "medium", "high")
AUTO = "auto/"
WINDOWS = ("five_hour", "weekly")
DEFAULT_MAX_USED = 98.0


class Limit(BaseModel):
    provider: str = Field(description="Provider name from providers.yaml, or * for all")
    window: str = Field(description="five_hour or weekly")
    max_used: float = Field(
        description="Skip the provider once this window is at least this % used"
    )


class RoutingRules(BaseModel):
    limits: list[Limit] = Field(default_factory=list)


@dataclass
class AgentChoice:
    agent: str
    model: str
    provider: str
    effort: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> AgentChoice:
        return cls(
            agent=str(data["agent"]),
            model=str(data.get("model") or ""),
            provider=str(data["provider"]),
            effort=str(data.get("effort") or ""),
        )


@dataclass
class Routing:
    """The ``tiers``, ``agents`` and ``routing`` parts of providers.yaml."""

    tiers: dict[str, list[str]] = field(default_factory=dict)
    agents: dict[str, list[AgentChoice]] = field(default_factory=dict)
    rules: RoutingRules = field(default_factory=RoutingRules)
    text: str = ""  # routing.md

    @property
    def text_sha(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest() if self.text else ""


def is_auto(ref: str) -> bool:
    return str(ref or "").startswith(AUTO)


def tier_of(ref: str) -> str:
    return str(ref).removeprefix(AUTO)


# -- usage windows ------------------------------------------------------------


def codex_windows(rate_limits: dict | None) -> dict:
    """Codex's rate-limit snapshot as {"five_hour"|"weekly": {used, resets_at}}."""
    out = {}
    for window in ((rate_limits or {}).get(k) for k in ("primary", "secondary")):
        if not window:
            continue
        minutes = window.get("windowDurationMins") or 0
        name = "five_hour" if minutes and minutes <= 24 * 60 else "weekly"
        out[name] = {
            "used": float(window.get("usedPercent") or 0),
            "resets_at": window.get("resetsAt"),
        }
    return out


def claude_windows(info: dict | None) -> dict:
    """Claude Code's ``rate_limit_info`` as {"five_hour"|"weekly": {used, resets_at}}."""
    info = info or {}
    names = {"five_hour": "five_hour", "seven_day": "weekly"}
    out = {}
    for key, window in (info.get("unifiedWindows") or {}).items():
        if key in names and window:
            out[names[key]] = {
                "used": _percent(window.get("utilization")),
                "resets_at": window.get("resetsAt"),
            }
    name = names.get(info.get("rateLimitType") or "")
    if name:
        used = _percent(info.get("utilization"))
        if info.get("status") == "rejected":
            used = 100.0
        if used is not None:
            out[name] = {"used": used, "resets_at": info.get("resetsAt")}
    return out


def _percent(value) -> float | None:
    if value is None:
        return None
    value = float(value)
    return value * 100 if value <= 1 else value


def record_usage_windows(provider: str, windows: dict) -> None:
    """Save the latest windows a provider's engine reported (runs the ORM)."""
    if not provider or not windows:
        return
    from django_ergo.conversation.models import ProviderUsage

    row, _ = ProviderUsage.objects.get_or_create(provider=provider)
    row.windows = {**(row.windows or {}), **windows}
    row.save(update_fields=["windows", "updated_at"])


async def arecord_usage_windows(provider: str, windows: dict) -> None:
    if not provider or not windows:
        return
    from asgiref.sync import sync_to_async

    try:
        await sync_to_async(record_usage_windows, thread_sensitive=True)(
            provider, windows
        )
    except Exception:  # noqa: BLE001 - usage tracking never fails a turn
        logger.warning("Couldn't record usage windows for %s", provider, exc_info=True)


def current_usage(now: float | None = None) -> dict[str, dict]:
    """{provider: {window: used %}}, leaving out windows that have reset."""
    from django_ergo.conversation.models import ProviderUsage

    now = time.time() if now is None else now
    usage: dict[str, dict] = {}
    for row in ProviderUsage.objects.all():
        for name, window in (row.windows or {}).items():
            resets = window.get("resets_at")
            if resets and resets <= now:
                continue
            usage.setdefault(row.provider, {})[name] = float(window.get("used") or 0)
    return usage


# -- picking ------------------------------------------------------------------


def active_rules(routing: Routing) -> RoutingRules:
    """routing.md's compiled rules when they're current, else the YAML rules."""
    if routing.text:
        from django_ergo.conversation.models import RoutingPolicy

        compiled = (
            RoutingPolicy.objects.filter(source_sha=routing.text_sha)
            .values_list("rules", flat=True)
            .first()
        )
        if compiled is not None:
            return RoutingRules.model_validate(compiled)
    return routing.rules


def over_limit(provider: str, usage: dict, rules: RoutingRules) -> bool:
    used = usage.get(provider) or {}
    limits = [r for r in rules.limits if r.provider in (provider, "*")]
    for window in WINDOWS:
        caps = [r.max_used for r in limits if r.window == window]
        if used.get(window, 0) >= min(caps, default=DEFAULT_MAX_USED):
            return True
    return False


def headroom(provider: str, usage: dict) -> float:
    return 100 - max((usage.get(provider) or {}).values(), default=0)


def choose(
    candidates: list, provider_of, usage: dict, rules: RoutingRules, current=None
):
    """The candidate to use: ``current`` while it qualifies, else the first
    one under its limits, else the one with the most room."""
    if not candidates:
        return None
    from django.utils.module_loading import import_string

    from django_ergo.settings import api_settings

    custom = getattr(api_settings, "MODEL_ROUTER", None)
    if custom:
        router = import_string(custom) if isinstance(custom, str) else custom
        return router(candidates, usage, rules, current)
    ok = [c for c in candidates if not over_limit(provider_of(c), usage, rules)]
    if current is not None and current in ok:
        return current
    if ok:
        return ok[0]
    return max(candidates, key=lambda c: headroom(provider_of(c), usage))


def pick_model(providers: Providers, tier: str, current: str = "") -> str:
    """A ``provider/model`` for a bot chat at ``tier`` (runs the ORM)."""
    routing = providers.routing
    listed = routing.tiers.get(tier)
    if not listed:
        msg = f"providers.yaml has no {tier!r} tier"
        raise ValueError(msg)
    candidates = [
        ref
        for ref in listed
        if (found := providers.find(ref)) is not None and found[0].available
    ]
    if not candidates:
        msg = f"No provider in the {tier!r} tier is available"
        raise ValueError(msg)
    return choose(
        candidates,
        lambda ref: ref.partition("/")[0],
        current_usage(),
        active_rules(routing),
        current if current in candidates else None,
    )


def pick_agent(providers: Providers, tier: str) -> AgentChoice:
    """A coding agent, model and effort for ``tier``, on a subscription only."""
    routing = providers.routing
    listed = routing.agents.get(tier)
    if not listed:
        msg = f"providers.yaml has no {tier!r} tier under agents"
        raise ValueError(msg)
    candidates = [
        c
        for c in listed
        if (p := providers.providers.get(c.provider)) is not None
        and p.transport == "cli"
    ]
    if not candidates:
        msg = f"No subscription provider in the {tier!r} agents tier"
        raise ValueError(msg)
    return choose(
        candidates, lambda c: c.provider, current_usage(), active_rules(routing)
    )


# -- routing.md -----------------------------------------------------------------

COMPILE_PROMPT = """\
You turn a deployment's model routing priorities, written in plain words, into
usage limits. Providers (name: type, transport, models):
{providers}

Windows: five_hour (the subscription's rolling 5-hour limit) and weekly.
Each limit says: stop routing to this provider once this window is at least
max_used percent used, so the next candidate in the tier is used instead.
Candidates are tried in the order their tier lists them, so preferences like
"Claude first" are already in the tiers; only write limits. Use provider "*"
for a limit on every provider. Write no limits the text doesn't ask for.
"""


def providers_summary(providers: Providers) -> str:
    return "\n".join(
        f"- {p.name}: {p.type}, {p.transport}, {', '.join(p.models)}"
        for p in providers.providers.values()
    )


async def compile_routing(
    providers: Providers, *, user=None, engine=None
) -> RoutingRules:
    """Compile routing.md into rules and store them (one structured call)."""
    from asgiref.sync import sync_to_async

    from django_ergo.conversation.models import RoutingPolicy
    from django_ergo.conversation.structured import StructuredCallSpec
    from django_ergo.conversation.structured import run_structured_call

    routing = providers.routing
    if not routing.text:
        return routing.rules
    spec = StructuredCallSpec(
        kind="routing_rules",
        system_prompt=COMPILE_PROMPT.format(providers=providers_summary(providers)),
        response_model=RoutingRules,
        max_turns=2,
    )
    result = await run_structured_call(
        spec, routing.text, user=user, engine=engine, metadata={"routing": "compile"}
    )
    if result.parsed is None:
        msg = f"Couldn't compile routing.md: {result.call.error or 'no rules returned'}"
        raise ValueError(msg)
    rules = result.parsed
    await sync_to_async(RoutingPolicy.objects.update_or_create)(
        source_sha=routing.text_sha,
        defaults={"source": routing.text, "rules": rules.model_dump()},
    )
    return rules


_compiling: set[str] = set()


def ensure_compiled(providers: Providers, make_engine) -> None:
    """Compile routing.md in the background if this version isn't stored yet
    (runs the ORM). Picks use the YAML rules until it's done."""
    from django_ergo.conversation.models import RoutingPolicy

    sha = providers.routing.text_sha
    if not sha or sha in _compiling:
        return
    if RoutingPolicy.objects.filter(source_sha=sha).exists():
        return
    _compiling.add(sha)

    def run():
        from django.db import close_old_connections

        try:
            asyncio.run(compile_routing(providers, engine=make_engine()))
        except Exception:  # noqa: BLE001 - the YAML rules stay in use
            logger.warning("Couldn't compile routing.md", exc_info=True)
            _compiling.discard(sha)  # try again on a later turn
        finally:
            close_old_connections()

    threading.Thread(target=run, name="ergo-routing-compile", daemon=True).start()


def routing_report(providers: Providers) -> dict:
    """What the router sees: tiers, rules in force, and each provider's usage."""
    from django_ergo.conversation.models import RoutingPolicy

    routing = providers.routing
    compiled = (
        RoutingPolicy.objects.filter(source_sha=routing.text_sha).exists()
        if routing.text
        else False
    )
    return {
        "tiers": routing.tiers,
        "agents": {
            tier: [vars(choice) for choice in choices]
            for tier, choices in routing.agents.items()
        },
        "text": routing.text,
        "compiled": compiled,
        "rules": active_rules(routing).model_dump(),
        "usage": current_usage(),
    }
