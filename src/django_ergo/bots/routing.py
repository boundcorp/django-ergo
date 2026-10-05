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
words (Ergonaut's Routing page can replace it per deployment, see
:func:`routing_text`) ("lean on Claude until its 5-hour window is 85% used, keep Codex's
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
WINDOW_NAMES = {"five_hour": "5-hour window", "weekly": "weekly window"}


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
        return text_sha(self.text)


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest() if text else ""


def routing_text(routing: Routing) -> str:
    """The priorities in force: the text saved on Ergonaut's Routing page
    (:class:`RoutingText`) while there is one, else routing.md. Runs the ORM."""
    from django_ergo.conversation.models import RoutingText

    saved = RoutingText.objects.values_list("text", flat=True).first()
    return (routing.text if saved is None else saved).strip()


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
    text = routing_text(routing)
    if text:
        from django_ergo.conversation.models import RoutingPolicy

        compiled = (
            RoutingPolicy.objects.filter(source_sha=text_sha(text))
            .values_list("rules", flat=True)
            .first()
        )
        if compiled is not None:
            return RoutingRules.model_validate(compiled)
    return routing.rules


def limit_of(provider: str, window: str, rules: RoutingRules) -> float:
    caps = [
        r.max_used
        for r in rules.limits
        if r.provider in (provider, "*") and r.window == window
    ]
    return min(caps, default=DEFAULT_MAX_USED)


def why_over(provider: str, usage: dict, rules: RoutingRules) -> str:
    """Why a provider is skipped ("claude 5-hour window at 87% (limit 85%)"),
    or "" while it's under every limit."""
    used = usage.get(provider) or {}
    for window in WINDOWS:
        cap = limit_of(provider, window, rules)
        if used.get(window, 0) >= cap:
            return (
                f"{provider} {WINDOW_NAMES[window]} at {used[window]:.0f}% "
                f"(limit {cap:g}%)"
            )
    return ""


def over_limit(provider: str, usage: dict, rules: RoutingRules) -> bool:
    return bool(why_over(provider, usage, rules))


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
    providers: Providers, *, text: str | None = None, user=None, engine=None
) -> RoutingRules:
    """Compile the routing text (routing.md, or the Routing page's) into
    rules and store them (one structured call)."""
    from asgiref.sync import sync_to_async

    from django_ergo.conversation.models import RoutingPolicy
    from django_ergo.conversation.structured import StructuredCallSpec
    from django_ergo.conversation.structured import run_structured_call

    routing = providers.routing
    if text is None:
        text = await sync_to_async(routing_text)(routing)
    if not text:
        return routing.rules
    spec = StructuredCallSpec(
        kind="routing_rules",
        system_prompt=COMPILE_PROMPT.format(providers=providers_summary(providers)),
        response_model=RoutingRules,
        max_turns=2,
    )
    result = await run_structured_call(
        spec, text, user=user, engine=engine, metadata={"routing": "compile"}
    )
    if result.parsed is None:
        msg = f"Couldn't compile routing.md: {result.call.error or 'no rules returned'}"
        raise ValueError(msg)
    rules = result.parsed
    await sync_to_async(RoutingPolicy.objects.update_or_create)(
        source_sha=text_sha(text),
        defaults={"source": text, "rules": rules.model_dump()},
    )
    return rules


_compiling: set[str] = set()
_failed: dict[str, str] = {}  # sha -> why it didn't compile, for the Routing page


def ensure_compiled(providers: Providers, make_engine, *, retry: bool = False) -> None:
    """Compile the routing text in the background if this version isn't
    stored yet (runs the ORM). Picks use the YAML rules until it's done. A
    version that failed to compile is tried again only with ``retry``."""
    from django_ergo.conversation.models import RoutingPolicy

    text = routing_text(providers.routing)
    sha = text_sha(text)
    if not sha or sha in _compiling or (sha in _failed and not retry):
        return
    if RoutingPolicy.objects.filter(source_sha=sha).exists():
        return
    _compiling.add(sha)
    _failed.pop(sha, None)

    def run():
        from django.db import close_old_connections

        try:
            asyncio.run(compile_routing(providers, text=text, engine=make_engine()))
        except Exception as exc:  # noqa: BLE001 - the YAML rules stay in use
            logger.warning("Couldn't compile routing.md", exc_info=True)
            _failed[sha] = str(exc)[:300]
        finally:
            _compiling.discard(sha)
            close_old_connections()

    threading.Thread(target=run, name="ergo-routing-compile", daemon=True).start()


# -- switches and the Routing page ----------------------------------------------


def switch_reason(
    providers: Providers, before: str, after: str, usage: dict, rules: RoutingRules
) -> str:
    """Why a chat moved from ``before`` to ``after`` (both provider/model)."""
    old = before.partition("/")[0]
    found = providers.find(before)
    if found is None or not found[0].available:
        return f"{before} isn't available"
    if why := why_over(old, usage, rules):
        return why
    return f"{after.partition('/')[0]} is back under its limits"


def record_switch(
    providers: Providers, session, tier: str, before: str, after: str
) -> None:
    """Log a chat's move to another model for the Routing page (runs the ORM)."""
    from django_ergo.conversation.models import RoutingSwitch

    reason = switch_reason(
        providers, before, after, current_usage(), active_rules(providers.routing)
    )
    meta = session.metadata or {}
    title = meta.get("title") or (
        "Main" if meta.get("bot_role") in ("root", "main") else "Thread"
    )
    RoutingSwitch.objects.create(
        session=session,
        label=f"{session.bot_name} · {title}"[:300],
        tier=tier,
        from_model=before,
        to_model=after,
        reason=reason[:300],
    )


def agent_label(choice: AgentChoice) -> str:
    return " · ".join(x for x in (choice.agent, choice.model, choice.effort) if x)


def record_agent_pick(
    providers: Providers, tier: str, choice: AgentChoice, label: str, session=None
) -> None:
    """Log a coding agent started on a fallback, i.e. not its tier's first
    choice (runs the ORM)."""
    from django_ergo.conversation.models import RoutingSwitch

    first = (providers.routing.agents.get(tier) or [None])[0]
    if first is None or first == choice:
        return
    usage, rules = current_usage(), active_rules(providers.routing)
    provider = providers.providers.get(first.provider)
    if provider is None or provider.transport != "cli":
        reason = f"{first.provider} isn't a subscription"
    else:
        reason = why_over(first.provider, usage, rules) or "picked by MODEL_ROUTER"
    RoutingSwitch.objects.create(
        session=session,
        label=label[:300],
        tier=tier,
        from_model=agent_label(first),
        to_model=agent_label(choice),
        reason=reason[:300],
    )


def _unavailable(provider) -> str:
    if provider.transport == "cli":
        cli = "Codex" if provider.type == "openai" else "Claude Code"
        return f"{cli} CLI isn't installed"
    return f"{provider.api_key_env} isn't set"


@dataclass
class _Now:
    """What a Routing page report is computed against."""

    providers: Providers
    usage: dict
    rules: RoutingRules

    def state(self, name: str, *, is_pick: bool = False) -> tuple[str, str]:
        provider = self.providers.providers.get(name)
        if provider is None:
            return "unavailable", f"{name} isn't in providers.yaml"
        if not provider.available:
            return "unavailable", _unavailable(provider)
        why = why_over(name, self.usage, self.rules)
        if is_pick:
            return "pick", why
        return ("skip", why) if why else ("ok", "")


def _tier_rows(now: _Now) -> list[dict]:
    from django_ergo.conversation.models import ConversationSession

    out = []
    for tier, refs in now.providers.routing.tiers.items():
        try:
            picked = pick_model(now.providers, tier)
        except ValueError:
            picked = ""
        rows = []
        for ref in refs:
            name = ref.partition("/")[0]
            state, reason = now.state(name, is_pick=ref == picked)
            found = now.providers.find(ref)
            label = (found[1].label or found[1].name) if found else ref
            rows.append(
                {
                    "provider": name,
                    "ref": ref,
                    "label": label,
                    "state": state,
                    "reason": reason,
                }
            )
        chats = ConversationSession.objects.filter(model=f"{AUTO}{tier}").count()
        out.append({"name": tier, "picked": picked, "chats": chats, "candidates": rows})
    return out


def _agent_rows(now: _Now) -> list[dict]:
    out = []
    for tier, choices in now.providers.routing.agents.items():
        try:
            picked = pick_agent(now.providers, tier)
        except ValueError:
            picked = None
        rows = []
        for choice in choices:
            state, reason = now.state(choice.provider, is_pick=choice == picked)
            rows.append(
                {
                    **vars(choice),
                    "label": agent_label(choice),
                    "state": state,
                    "reason": reason,
                }
            )
        out.append({"name": tier, "candidates": rows})
    return out


def _provider_rows(now: _Now, in_use: set[str]) -> list[dict]:
    from django_ergo.conversation.models import ProviderUsage

    reported = {row.provider: row for row in ProviderUsage.objects.all()}
    clock = time.time()
    out = []
    for name, provider in now.providers.providers.items():
        row = reported.get(name)
        windows = {}
        for window in WINDOWS:
            seen = ((row.windows or {}) if row else {}).get(window) or {}
            resets = seen.get("resets_at")
            fresh = bool(seen) and not (resets and resets <= clock)
            windows[window] = {
                "used": float(seen.get("used") or 0) if fresh else None,
                "resets_at": resets if fresh else None,
                "limit": limit_of(name, window, now.rules),
            }
        state, reason = now.state(name)
        status = {"unavailable": "unavailable", "skip": "skipped"}.get(state)
        if status is None:
            status = (
                "in_use"
                if name in in_use
                else ("standby" if provider.transport == "cli" else "api_key")
            )
        out.append(
            {
                "name": name,
                "type": provider.type,
                "transport": provider.transport,
                "subscription": provider.transport == "cli",
                "api_key_env": provider.api_key_env,
                "status": status,
                "reason": reason,
                "windows": windows,
                "reported_at": row.updated_at.isoformat() if row else None,
            }
        )
    return out


def routing_report(providers: Providers) -> dict:
    """What the router sees and would pick now, for Ergonaut's Routing page:
    each provider's windows against its limits, every tier's candidates, the
    routing text and whether it's compiled, and recent switches."""
    from django_ergo.conversation.models import RoutingPolicy
    from django_ergo.conversation.models import RoutingSwitch
    from django_ergo.conversation.models import RoutingText

    routing = providers.routing
    saved = RoutingText.objects.first()
    text = routing_text(routing)
    sha = text_sha(text)
    now = _Now(providers, current_usage(), active_rules(routing))
    tiers, agents = _tier_rows(now), _agent_rows(now)
    in_use = {
        c["provider"]
        for t in tiers + agents
        for c in t["candidates"]
        if c["state"] == "pick"
    }
    source = "page" if saved is not None else ("file" if routing.text else "")
    return {
        "providers": _provider_rows(now, in_use),
        "tiers": tiers,
        "agents": agents,
        "text": text,
        "text_source": source,
        "file_text": routing.text,
        "updated_at": saved.updated_at.isoformat() if saved else None,
        "compiled": bool(sha) and RoutingPolicy.objects.filter(source_sha=sha).exists(),
        "compiling": sha in _compiling,
        "compile_error": _failed.get(sha, ""),
        "rules": now.rules.model_dump(),
        "default_max_used": DEFAULT_MAX_USED,
        "usage": now.usage,
        "switches": [
            {
                "at": switch.created_at.isoformat(),
                "session_id": str(switch.session_id) if switch.session_id else None,
                "label": switch.label,
                "tier": switch.tier,
                "from": switch.from_model,
                "to": switch.to_model,
                "reason": switch.reason,
            }
            for switch in RoutingSwitch.objects.all()[:20]
        ],
    }
