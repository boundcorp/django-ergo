"""Model routing: built-in and custom tiers from each subscription's headroom.

``providers.yaml`` lists each tier's candidates in order of preference, for
bot chats (``tiers``) and for coding agents started through Orca
(``agents``)::

    tiers:
      small:  [subscription/claude-haiku-5-5, chatgpt/gpt-6-luna]
      medium: [subscription/claude-sonnet-5-5, chatgpt/gpt-6.1-sol]
      large:  [subscription/claude-opus-5-5, chatgpt/gpt-6-sol]
      xlarge: [subscription/claude-fable-5-1, chatgpt/gpt-6-astra,
               subscription/claude-opus-5-5, chatgpt/gpt-6-sol]
    agents:                      # subscriptions only: a candidate names its provider
      medium:
        - {agent: claude, model: claude-sonnet-5-5, provider: subscription}
        - {agent: codex, model: gpt-6.1-sol, effort: medium, provider: chatgpt}
    routing:                     # optional rules; routing.md can say it in words
      limits:
        - {provider: subscription, window: five_hour, max_used: 85}
        - {provider: chatgpt, window: weekly, max_used: 80}

The built-in tiers are small, medium, large and xlarge. The old names ``low``
and ``high`` still work as aliases of small and large (:func:`tier_of`), in
``auto/<tier>`` refs and as providers.yaml keys.

A chat (or a bot's ``engine.model``) set to ``auto/<tier>`` gets a model per
turn from :func:`pick_model`: the first candidate whose provider is
available and under every limit. A chat keeps the model it had while that
model still qualifies, so a prompt cache isn't thrown away for nothing.
When every candidate is over a limit, the one whose subscription has the
most room wins. Agent candidates must be CLI subscriptions, never API keys.

Limits read :class:`ProviderUsage`: only the windows each CLI reports
(Claude Code's ``rate_limit_event``, Codex's ``account/rateLimits``), including
Claude's separate Fable weekly window, applied only to Fable models.
A provider that refused a call counts that window as fully used until reset.

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

TIERS = ("small", "medium", "large", "xlarge")
# Names the tiers had before small/large; existing auto/low chats keep working.
TIER_ALIASES = {"low": "small", "high": "large"}
# Reasoning effort is chosen apart from the tier: a chat's effort setting
# (Bot.pick_effort), else medium, on every engine that takes one.
EFFORTS = ("low", "medium", "high", "xhigh")
DEFAULT_EFFORT = "medium"
AUTO = "auto/"
DEFAULT_MAX_USED = 98.0
USAGE_STALE_SECONDS = 15 * 60  # three missed 5-minute syncs
WINDOW_NAMES = {
    "five_hour": "5-hour window",
    "weekly": "weekly window",
    "weekly_fable": "7-day Fable window",
}


class Limit(BaseModel):
    provider: str = Field(description="Provider name from providers.yaml, or * for all")
    window: str = Field(
        description="Reported window ID, e.g. five_hour, weekly, weekly_fable"
    )
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
    # Recompile pre-window-aware policies rather than retaining engine-name rules.
    return hashlib.sha256(f"routing-v2:{text}".encode()).hexdigest() if text else ""


def routing_text(routing: Routing) -> str:
    """The priorities in force: the text saved on Ergonaut's Routing page
    (:class:`RoutingText`) while there is one, else routing.md. Runs the ORM."""
    from django_ergo.conversation.models import RoutingText

    saved = RoutingText.objects.values_list("text", flat=True).first()
    return (routing.text if saved is None else saved).strip()


def is_auto(ref: str) -> bool:
    return str(ref or "").startswith(AUTO)


def tier_of(ref: str) -> str:
    """The tier named by ``auto/<tier>`` (or a bare tier), aliases resolved."""
    tier = str(ref).removeprefix(AUTO)
    return TIER_ALIASES.get(tier, tier)


# -- usage windows ------------------------------------------------------------


def window_period(name: str) -> str:
    """The length a window name stands for: ``5h``, ``7d``, or "" when unknown."""
    if name == "five_hour":
        return "5h"
    if name == "weekly" or name.startswith("weekly_"):
        return "7d"
    return ""


def _window(name: str, used, resets, **metadata) -> dict:
    return {
        "label": metadata.get("label")
        or WINDOW_NAMES.get(name, name.replace("_", " ")).removesuffix(" window"),
        "used": used,
        "remaining": 100 - used if used is not None else None,
        "resets_at": resets,
        "status": metadata.get("status", ""),
        "model": metadata.get("model", ""),
        "period": metadata.get("period") or window_period(name),
        "observed_at": metadata.get("observed_at"),
    }


def codex_windows(rate_limits: dict | None) -> dict:
    """Only windows in Codex's snapshot; primary need not be a 5-hour limit."""
    out = {}
    for key in ("primary", "secondary"):
        window = (rate_limits or {}).get(key)
        if not window:
            continue
        minutes = window.get("windowDurationMins")
        name = {300: "five_hour", 10080: "weekly"}.get(minutes)
        name = name or (f"minutes_{minutes}" if minutes else key)
        label = window.get("label") or (
            f"{minutes} minutes" if minutes and name.startswith("minutes_") else ""
        )
        used = window.get("usedPercent")
        out[name] = _window(
            name,
            float(used) if used is not None else None,
            window.get("resetsAt"),
            label=label,
            status=window.get("status") or "",
        )
    return out


def claude_windows(info: dict | None) -> dict:
    """Preserve every reported Claude window, including model-specific limits."""
    info = info or {}
    names = {"seven_day": "weekly"}
    out = {}
    for key, window in (info.get("unifiedWindows") or {}).items():
        if not window:
            continue
        name = names.get(key, key.replace("seven_day_", "weekly_", 1))
        model = key.removeprefix("seven_day_") if key.startswith("seven_day_") else ""
        out[name] = _window(
            name,
            _percent(window.get("utilization")),
            window.get("resetsAt"),
            label=window.get("label") or "",
            status=window.get("status") or "",
            model=model,
        )
    key = info.get("rateLimitType") or ""
    if key:
        name = names.get(key, key.replace("seven_day_", "weekly_", 1))
        used = _percent(info.get("utilization"))
        if info.get("status") == "rejected":
            used = 100.0
        if used is not None:
            model = (
                key.removeprefix("seven_day_") if key.startswith("seven_day_") else ""
            )
            out[name] = _window(
                name,
                used,
                info.get("resetsAt"),
                status=info.get("status") or "",
                model=model,
            )
    return out


def _percent(value) -> float | None:
    if value is None:
        return None
    value = float(value)
    return value * 100 if value <= 1 else value


def record_usage_windows(
    provider: str, windows: dict, *, full_snapshot: bool = False
) -> None:
    """Replace authoritative snapshots; merge partial per-call rate-limit events.

    Every window carries the time it was observed, so a partial event that
    refreshes one window never makes the others it leaves alone look current."""
    if not provider or (not windows and not full_snapshot):
        return
    from django_ergo.conversation.models import ProviderUsage

    seen = time.time()
    windows = {
        name: {**window, "observed_at": window.get("observed_at") or seen}
        for name, window in windows.items()
    }
    row, _ = ProviderUsage.objects.get_or_create(provider=provider)
    row.windows = windows if full_snapshot else {**(row.windows or {}), **windows}
    row.save(update_fields=["windows", "updated_at"])


async def arecord_usage_windows(
    provider: str, windows: dict, *, full_snapshot: bool = False
) -> None:
    if not provider or (not windows and not full_snapshot):
        return
    from asgiref.sync import sync_to_async

    try:
        await sync_to_async(record_usage_windows, thread_sensitive=True)(
            provider, windows, full_snapshot=full_snapshot
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
            if window.get("used") is not None:
                usage.setdefault(row.provider, {})[name] = float(window["used"])
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


def applicable_usage(provider: str, usage: dict, model: str = "") -> dict:
    """Model-specific weekly limits never disqualify unrelated models."""
    return {
        name: used
        for name, used in (usage.get(provider) or {}).items()
        if not name.startswith("weekly_")
        or (model and name.removeprefix("weekly_") in model.lower())
    }


def why_over(provider: str, usage: dict, rules: RoutingRules, model: str = "") -> str:
    """Why this candidate is skipped, or "" while under its applicable limits."""
    for window, used in applicable_usage(provider, usage, model).items():
        cap = limit_of(provider, window, rules)
        if used >= cap:
            return (
                f"{provider} {WINDOW_NAMES.get(window, window.replace('_', ' '))} "
                f"at {used:.0f}% (limit {cap:g}%)"
            )
    return ""


def over_limit(
    provider: str, usage: dict, rules: RoutingRules, model: str = ""
) -> bool:
    return bool(why_over(provider, usage, rules, model))


def headroom(provider: str, usage: dict, model: str = "") -> float:
    return 100 - max(applicable_usage(provider, usage, model).values(), default=0)


def choose(  # noqa: PLR0913 - preserve custom-router/current contract, add candidate model scope
    candidates: list,
    provider_of,
    usage: dict,
    rules: RoutingRules,
    current=None,
    model_of=lambda c: "",
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
    ok = [
        c
        for c in candidates
        if not over_limit(provider_of(c), usage, rules, model_of(c))
    ]
    if current is not None and current in ok:
        return current
    if ok:
        return ok[0]
    return max(candidates, key=lambda c: headroom(provider_of(c), usage, model_of(c)))


def pick_model(providers: Providers, tier: str, current: str = "") -> str:
    """A ``provider/model`` for a bot chat at ``tier`` (runs the ORM)."""
    tier = tier_of(tier)
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
        model_of=lambda ref: ref.partition("/")[2],
    )


def pick_agent(providers: Providers, tier: str) -> AgentChoice:
    """A coding agent, model and effort for ``tier``, on a subscription only."""
    tier = tier_of(tier)
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
        candidates,
        lambda c: c.provider,
        current_usage(),
        active_rules(routing),
        model_of=lambda c: c.model,
    )


# -- routing.md -----------------------------------------------------------------

COMPILE_PROMPT = """\
You turn a deployment's model routing priorities, written in plain words, into
usage limits. Providers (name: type, transport, models):
{providers}

Windows are provider-reported IDs: five_hour (5 hours), weekly (7 days),
weekly_fable (7 days for Fable models only), or another reported window ID.
Use the configured provider NAME exactly, not its engine type. GPT, ChatGPT
and Codex mean the OpenAI CLI subscription, NOT an OpenAI API-key provider.
Each limit says: stop routing to this provider once this window is at least
max_used percent used, so the next candidate in the tier is used instead.
Candidates are tried in the order their tier lists them, so preferences like
"Claude first" are already in the tiers; only write limits. Use provider "*"
for a limit on every provider. Write no limits the text doesn't ask for.
"""


def providers_summary(providers: Providers) -> str:
    return "\n".join(
        f"- {p.name}: {p.type}, {p.transport}, {', '.join(p.models)}; "
        + (
            "ChatGPT/Codex/GPT subscription"
            if p.type == "openai" and p.transport == "cli"
            else "Claude subscription"
            if p.transport == "cli"
            else "pay-per-token API, NOT a subscription"
        )
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
    for rule in rules.limits:
        if rule.provider != "*" and rule.provider not in providers.providers:
            msg = f"Unknown routing provider {rule.provider!r}; use a configured provider name"
            raise ValueError(msg)
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
    if why := why_over(old, usage, rules, before.partition("/")[2]):
        return why
    return f"{after.partition('/')[0]} is back under its limits"


def record_switch(  # noqa: PLR0913
    providers: Providers, session, tier: str, before: str, after: str, reason: str = ""
) -> None:
    """Log a chat's move to another model for the Routing page (runs the ORM)."""
    from django_ergo.conversation.models import RoutingSwitch

    reason = reason or switch_reason(
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


# Substrings of a failed turn's error that mean a provider refused it for a
# subscription or rate limit.
LIMIT_ERRORS = (
    "usage limit",
    "rate limit",
    "rate_limit",
    "limit reached",
    "hit your limit",
    "429",
)


def is_limit_error(error: str) -> bool:
    error = (error or "").lower()
    return any(needle in error for needle in LIMIT_ERRORS)


def retry_model(providers: Providers, tier: str, failed: str, error: str) -> str:
    """Offer an under-limit alternative, including the same subscription when
    only a model-specific limit was reached. Nothing retries automatically."""
    tier = tier_of(tier)
    routing = providers.routing
    usage, rules = current_usage(), active_rules(routing)
    failed_provider = failed.partition("/")[0]
    if not (
        is_limit_error(error)
        or over_limit(failed_provider, usage, rules, failed.partition("/")[2])
    ):
        return ""
    scoped_limit = over_limit(
        failed_provider, usage, rules, failed.partition("/")[2]
    ) and not over_limit(failed_provider, usage, rules)
    for ref in routing.tiers.get(tier, []):
        name = ref.partition("/")[0]
        found = providers.find(ref)
        if (
            ref == failed
            or (name == failed_provider and not scoped_limit)
            or found is None
            or not found[0].available
        ):
            continue
        if not over_limit(name, usage, rules, ref.partition("/")[2]):
            return ref
    return ""


def agent_label(choice: AgentChoice) -> str:
    return " · ".join(x for x in (choice.agent, choice.model, choice.effort) if x)


def record_agent_pick(
    providers: Providers, tier: str, choice: AgentChoice, label: str, session=None
) -> None:
    """Log a coding agent started on a fallback, i.e. not its tier's first
    choice (runs the ORM)."""
    from django_ergo.conversation.models import RoutingSwitch

    tier = tier_of(tier)
    first = (providers.routing.agents.get(tier) or [None])[0]
    if first is None or first == choice:
        return
    usage, rules = current_usage(), active_rules(providers.routing)
    provider = providers.providers.get(first.provider)
    if provider is None or provider.transport != "cli":
        reason = f"{first.provider} isn't a subscription"
    else:
        reason = (
            why_over(first.provider, usage, rules, first.model)
            or "picked by MODEL_ROUTER"
        )
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

    def state(
        self, name: str, *, model: str = "", is_pick: bool = False
    ) -> tuple[str, str]:
        provider = self.providers.providers.get(name)
        if provider is None:
            return "unavailable", f"{name} isn't in providers.yaml"
        if not provider.available:
            return "unavailable", _unavailable(provider)
        why = why_over(name, self.usage, self.rules, model)
        if is_pick:
            return "pick", why
        return ("skip", why) if why else ("ok", "")


def _tier_rows(now: _Now) -> list[dict]:
    from django_ergo.conversation.models import ConversationSession

    out = []
    for tier, refs in now.providers.routing.tiers.items():
        if not refs:
            continue
        try:
            picked = pick_model(now.providers, tier)
        except ValueError:
            picked = ""
        rows = []
        for ref in refs:
            name = ref.partition("/")[0]
            state, reason = now.state(
                name, model=ref.partition("/")[2], is_pick=ref == picked
            )
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
        if not choices:
            continue
        try:
            picked = pick_agent(now.providers, tier)
        except ValueError:
            picked = None
        rows = []
        for choice in choices:
            state, reason = now.state(
                choice.provider, model=choice.model, is_pick=choice == picked
            )
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


def window_view(name: str, seen: dict, clock: float) -> dict:
    """A stored window as the Routing page shows it: a window past its reset
    has no usage until the next report, and a window is stale once its own
    observation (not the row's last write) is old."""
    resets = seen.get("resets_at")
    expired = bool(resets and resets <= clock)
    used = seen.get("used") if not expired else None
    observed = seen.get("observed_at")
    return {
        **seen,
        "label": seen.get("label")
        or WINDOW_NAMES.get(name, name.replace("_", " ")).removesuffix(" window"),
        "used": used,
        "remaining": 100 - used if used is not None else None,
        "resets_at": resets,
        "status": "reset" if expired else seen.get("status", ""),
        "period": seen.get("period") or window_period(name),
        "observed_at": observed,
        "stale": observed is None or clock - observed > USAGE_STALE_SECONDS,
    }


def _provider_rows(now: _Now, in_use: set[str]) -> list[dict]:
    from django_ergo.conversation.models import ProviderUsage

    reported = {row.provider: row for row in ProviderUsage.objects.all()}
    clock = time.time()
    out = []
    for name, provider in now.providers.providers.items():
        row = reported.get(name)
        windows = {
            window: {
                **window_view(window, seen, clock),
                "limit": limit_of(name, window, now.rules),
            }
            for window, seen in ((row.windows or {}) if row else {}).items()
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
                "stale": any(
                    w["stale"] for w in windows.values() if w["status"] != "reset"
                ),
            }
        )
    return out


def routing_report(providers: Providers) -> dict:
    """What the router sees and would pick now, for Ergonaut's Routing page:
    each provider's windows against its limits, every tier's candidates, the
    routing text and whether it's compiled, and recent switches."""
    from django_ergo.bots.usage_sync import capacity_report
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
        "capacity": capacity_report(providers, now.rules),
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
