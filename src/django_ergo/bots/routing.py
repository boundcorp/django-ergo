"""Capability-tier routing by subscription pace, shared by chats and workers.

Tier lists define eligible models and capability/cost tie-break order, not a
provider preference. Weekly elapsed-minus-used quota drives the score, with
an urgency bonus for unused quota near reset. Short windows more than ten
percentage points ahead of pace, and exhausted windows, exclude a candidate.
Configured percentage caps still apply; when all candidates exceed a soft
cap the best non-exhausted, short-window-safe candidate remains eligible.

``current_usage`` preserves the numeric mapping passed to custom MODEL_ROUTER
callbacks, carrying timing metadata separately. Legacy tier lists, aliases,
explicit models, percentage limits and custom router signatures still work.
``routing.soft_preferences`` adds a bounded, optional fractional-score nudge.
Picks and the Routing page expose the same pace calculation and reasons.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from pydantic import BaseModel
from pydantic import Field

from django_ergo.conversation import roles
from django_ergo.conversation.usage_windows import WINDOW_NAMES
from django_ergo.conversation.usage_windows import arecord_usage_windows  # noqa: F401
from django_ergo.conversation.usage_windows import claude_windows  # noqa: F401
from django_ergo.conversation.usage_windows import codex_windows  # noqa: F401
from django_ergo.conversation.usage_windows import record_usage_windows  # noqa: F401
from django_ergo.conversation.usage_windows import window_period
from django_ergo.daemon import start_daemon

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
EXHAUSTED_PERCENT = 100
WEEK_SECONDS = 7 * 24 * 3600
SHORT_PACE_MARGIN = 0.10


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
    soft_preferences: dict[str, float] = Field(default_factory=dict)


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
    # Recompile policies whose prompt described provider-first selection.
    return hashlib.sha256(f"routing-v3:{text}".encode()).hexdigest() if text else ""


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


class _Usage(dict):
    """Numeric usage for custom routers, plus timing for the built-in scorer."""

    def __init__(self):
        super().__init__()
        self.windows: dict[str, dict] = {}


def current_usage(now: float | None = None) -> dict[str, dict]:
    """{provider: {window: used %}}, with timing metadata in ``.windows``."""
    from django_ergo.conversation.models import ProviderUsage

    now = time.time() if now is None else now
    usage = _Usage()
    for row in ProviderUsage.objects.all():
        for name, window in (row.windows or {}).items():
            resets = window.get("resets_at")
            if resets and resets <= now:
                continue
            if window.get("used") is not None:
                usage.setdefault(row.provider, {})[name] = float(window["used"])
                usage.windows.setdefault(row.provider, {})[name] = window
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
    """Model-specific windows never disqualify unrelated models."""
    windows = getattr(usage, "windows", {})
    out = {}
    for name, used in (usage.get(provider) or {}).items():
        details = windows.get(provider, {}).get(name)
        if details is None and isinstance(used, dict):
            details = used
        scope = (
            details["model"]
            if details is not None and "model" in details
            else name.removeprefix("weekly_")
            if name.startswith("weekly_")
            else ""
        )
        if scope and scope.lower() not in model.lower():
            continue
        value = used.get("used") if isinstance(used, dict) else used
        if value is not None:
            out[name] = value
    return out


def window_pace(window: dict, *, now: float | None = None) -> dict:
    """Fractions, not percentages; unknown/stale timing earns no urgency."""
    now = time.time() if now is None else now
    used = window.get("used")
    resets = window.get("resets_at")
    period = window.get("period_seconds") or window.get("period")
    seconds = {"5h": 18000, "7d": 604800, "1w": 604800}.get(period)
    if seconds is None and isinstance(period, int | float):
        seconds = period
    hours = max(0.0, (resets - now) / 3600) if resets else None
    elapsed = None
    if seconds and seconds > 0 and resets and resets > now:
        elapsed = min(1.0, max(0.0, 1 - (resets - now) / seconds))
    used_frac = used / 100 if used is not None else None
    return {
        "elapsed_frac": elapsed,
        "used_frac": used_frac,
        "pace_delta": elapsed - used_frac
        if elapsed is not None and used_frac is not None
        else None,
        "hours_to_reset": hours,
    }


def _paced_windows(provider: str, usage: dict, model: str, now: float):
    details = getattr(usage, "windows", usage).get(provider, {})
    for name in applicable_usage(provider, usage, model):
        window = details.get(name)
        if not isinstance(window, dict):
            continue
        observed = window.get("observed_at")
        if observed is None or now - observed > USAGE_STALE_SECONDS:
            continue
        window = {"period": window_period(name), **window}
        pace = window_pace(window, now=now)
        if pace["pace_delta"] is not None:
            yield name, window, pace


def pace_block(provider: str, usage: dict, model: str = "") -> str:
    """Hard constraints, never bypassed by the all-over-soft-caps fallback."""
    for name, used in applicable_usage(provider, usage, model).items():
        if used >= EXHAUSTED_PERCENT:
            return f"{provider} {name} exhausted"
    for name, window, pace in _paced_windows(provider, usage, model, time.time()):
        period = window.get("period_seconds") or window.get("period")
        weekly = period in ("7d", "1w") or (
            isinstance(period, int | float) and period >= WEEK_SECONDS
        )
        if not weekly and pace["pace_delta"] < -SHORT_PACE_MARGIN:
            return f"{provider} {name} {-100 * pace['pace_delta']:.0f}% ahead of pace"
    return ""


def pace_score(
    provider: str,
    usage: dict,
    rules: RoutingRules,
    model: str = "",
    *,
    now: float | None = None,
) -> float:
    """Worst weekly window dominates; short windows constrain, never reward."""
    weekly, short = [], []
    now = time.time() if now is None else now
    for _name, window, pace in _paced_windows(provider, usage, model, now):
        delta = pace["pace_delta"]
        period = window.get("period_seconds") or window.get("period")
        is_weekly = period in ("7d", "1w") or (
            isinstance(period, int | float) and period >= WEEK_SECONDS
        )
        if is_weekly:
            urgency = 1 + min(7.0, 24 / max(1.0, pace["hours_to_reset"]))
            weekly.append(delta * urgency if delta > 0 else 2 * delta)
        else:
            short.append(2 * min(0.0, delta))
    preference = max(-0.1, min(0.1, rules.soft_preferences.get(provider, 0.0)))
    return min(weekly, default=0.0) + min(short, default=0.0) + preference


def pick_reason(
    provider: str, usage: dict, rules: RoutingRules, model: str = ""
) -> str:
    windows = list(_paced_windows(provider, usage, model, time.time()))
    if not windows:
        return f"chose {provider}: pace unavailable; capability/cost tie-break"
    # Explain the weekly budget first; short-window constraints are separate.
    name, _window, pace = next((w for w in windows if w[0] == "weekly"), windows[0])
    delta = pace["pace_delta"]
    return (
        f"chose {provider}: {abs(delta) * 100:.0f}% "
        f"{'under' if delta >= 0 else 'ahead of'} {name.replace('_', ' ')} pace, "
        f"resets in {pace['hours_to_reset']:.1f}h "
        f"(score {pace_score(provider, usage, rules, model):.2f})"
    )


def why_over(provider: str, usage: dict, rules: RoutingRules, model: str = "") -> str:
    """Why this candidate is skipped, or "" while under its applicable limits."""
    if blocked := pace_block(provider, usage, model):
        return blocked
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


def choose(  # noqa: PLR0913 - preserve custom-router/current contract, add candidate model scope
    candidates: list,
    provider_of,
    usage: dict,
    rules: RoutingRules,
    current=None,
    model_of=lambda c: "",
):
    """Best pace among safe candidates; cache affinity breaks exact ties only."""
    if not candidates:
        return None
    from django.utils.module_loading import import_string

    from django_ergo.settings import api_settings

    custom = getattr(api_settings, "MODEL_ROUTER", None)
    if custom:
        router = import_string(custom) if isinstance(custom, str) else custom
        picked = router(candidates, usage, rules, current)
        if picked is not None and picked not in candidates:
            # A router picks among the candidates it was given; anything else
            # (another provider, a private model) would bypass the tier.
            msg = f"MODEL_ROUTER returned {picked!r}, which isn't one of its candidates"
            raise ValueError(msg)
        return picked
    safe = [c for c in candidates if not pace_block(provider_of(c), usage, model_of(c))]
    ok = [c for c in safe if not over_limit(provider_of(c), usage, rules, model_of(c))]
    eligible = ok or safe
    if not eligible:
        return None
    clock = time.time()  # All candidates share one instant, including exact ties.
    picked = max(
        eligible,
        key=lambda c: (
            pace_score(provider_of(c), usage, rules, model_of(c), now=clock),
            c == current,
        ),
    )
    logger.info("%s", pick_reason(provider_of(picked), usage, rules, model_of(picked)))
    return picked


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
        if not providers.is_private(ref)
        and (found := providers.find(ref)) is not None
        and found[0].available
    ]
    if not candidates:
        msg = f"No provider in the {tier!r} tier is available"
        raise ValueError(msg)
    picked = choose(
        candidates,
        lambda ref: ref.partition("/")[0],
        current_usage(),
        active_rules(routing),
        current if current in candidates else None,
        model_of=lambda ref: ref.partition("/")[2],
    )
    if picked is None:
        msg = f"No safe provider in the {tier!r} tier"
        raise ValueError(msg)
    return providers.check_public(picked, where=f"auto/{tier}")


def pick_agent(
    providers: Providers,
    tier: str,
    agents: tuple[str, ...] = (),
    *,
    receipt: dict | None = None,
) -> AgentChoice:
    """A coding agent, model and effort for ``tier``, on a subscription only.
    ``agents`` (a manager's) keeps the candidates it can run; empty means any."""
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
        and not providers.is_private(c.model)
    ]
    if not candidates:
        msg = f"No subscription provider in the {tier!r} agents tier"
        raise ValueError(msg)
    if agents:
        runnable = [c for c in candidates if c.agent in agents]
        if not runnable:
            listed_agents = ", ".join(sorted({c.agent for c in candidates}))
            msg = (
                f"The {tier!r} agents tier lists {listed_agents}, and this manager "
                f"runs {', '.join(agents)}: add {' or '.join(agents)} candidates to "
                "the tier in providers.yaml"
            )
            raise ValueError(msg)
        candidates = runnable
    usage = current_usage()
    rules = active_rules(routing)
    picked = choose(
        candidates,
        lambda c: c.provider,
        usage,
        rules,
        model_of=lambda c: c.model,
    )
    if picked is None:
        msg = f"No safe subscription in the {tier!r} agents tier"
        raise ValueError(msg)
    if receipt is not None:
        from django_ergo.settings import api_settings

        now = time.time()
        windows = getattr(usage, "windows", {}).get(picked.provider, {})
        applicable = applicable_usage(picked.provider, usage, picked.model)
        receipt.update(
            {
                "source": "provider_usage",
                "selected_at": now,
                "reason": (
                    "custom router; reason unavailable"
                    if getattr(api_settings, "MODEL_ROUTER", None)
                    else "subscription pace routing"
                    if any(_paced_windows(picked.provider, usage, picked.model, now))
                    else "pace unavailable; capability/cost tie-break"
                ),
                "windows": [
                    {
                        "name": name if name in WINDOW_NAMES else "other",
                        "used": w.get("used"),
                        "observed_at": w.get("observed_at"),
                        "resets_at": w.get("resets_at"),
                        "period_seconds": w.get("period_seconds"),
                        "fresh": w.get("observed_at") is not None
                        and now - w["observed_at"] <= USAGE_STALE_SECONDS,
                    }
                    for name, w in windows.items()
                    if name in applicable
                ],
            }
        )
    providers.check_public(picked.model, where=f"The {tier} agents tier")
    return picked


# -- routing.md -----------------------------------------------------------------

COMPILE_PROMPT = """\
You turn a deployment's model routing priorities into optional percentage caps
and soft_preferences (provider name to a small fractional-score nudge).
Providers (name: type, transport, models):
{providers}

The router ALREADY scores elapsed fraction minus used fraction for every
window, prioritizes weekly budgets with unused quota expiring soon, excludes
exhausted windows and guards short windows more than 10 points over pace.
Do not invent raw percentage caps for pace instructions. Weekly 23% used with
one hour left is preferable to weekly 5% used just after reset.
Use configured provider NAME exactly. GPT/ChatGPT/Codex means the OpenAI CLI
subscription, NOT an API-key provider. Windows include five_hour, weekly,
weekly_fable (only Fable), and other reported IDs.
Only explicit maximum-used instructions become limits. Provider preference is
optional soft_preferences, bounded -0.1 to 0.1; never encode it in tier order.
Return no limits/preferences the text does not request.
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
        try:
            asyncio.run(compile_routing(providers, text=text, engine=make_engine()))
        except Exception as exc:  # noqa: BLE001 - the YAML rules stay in use
            logger.warning("Couldn't compile routing.md", exc_info=True)
            _failed[sha] = str(exc)[:300]
        finally:
            _compiling.discard(sha)

    start_daemon(run, name="ergo-routing-compile")


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
    return pick_reason(after.partition("/")[0], usage, rules, after.partition("/")[2])


def record_switch(  # noqa: PLR0913
    providers: Providers, session, tier: str, before: str, after: str, reason: str = ""
) -> None:
    """Log a chat's move to another model for the Routing page (runs the ORM)."""
    from django_ergo.conversation.models import RoutingSwitch

    reason = reason or switch_reason(
        providers, before, after, current_usage(), active_rules(providers.routing)
    )
    title = roles.title(session)
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


def is_refusal_error(error: str) -> bool:
    """A turn the model's safety system declined (see structured.REFUSAL_ERROR)."""
    return "(refusal" in (error or "")


def retry_model(providers: Providers, tier: str, failed: str, error: str) -> str:
    """Offer an under-limit alternative, including the same subscription when
    only a model-specific limit was reached or the model refused the turn.
    Nothing retries automatically."""
    tier = tier_of(tier)
    routing = providers.routing
    usage, rules = current_usage(), active_rules(routing)
    failed_provider = failed.partition("/")[0]
    refused = is_refusal_error(error)
    if not (
        refused
        or is_limit_error(error)
        or over_limit(failed_provider, usage, rules, failed.partition("/")[2])
    ):
        return ""
    # A refusal or a model-specific limit leaves the provider's other models.
    scoped_limit = refused or (
        over_limit(failed_provider, usage, rules, failed.partition("/")[2])
        and not over_limit(failed_provider, usage, rules)
    )
    candidates = []
    for ref in routing.tiers.get(tier, []):
        name = ref.partition("/")[0]
        found = providers.find(ref)
        if (
            ref == failed
            or (name == failed_provider and not scoped_limit)
            or found is None
            or not found[0].available
            or providers.is_private(ref)
            or over_limit(name, usage, rules, ref.partition("/")[2])
        ):
            continue
        candidates.append(ref)
    return (
        choose(
            candidates,
            lambda ref: ref.partition("/")[0],
            usage,
            rules,
            model_of=lambda ref: ref.partition("/")[2],
        )
        or ""
    )


def agent_label(choice: AgentChoice) -> str:
    return " · ".join(x for x in (choice.agent, choice.model, choice.effort) if x)


def record_agent_pick(
    providers: Providers, tier: str, choice: AgentChoice, label: str, session=None
) -> None:
    """Log every coding-agent tier selection and its pace reason."""
    from django_ergo.conversation.models import RoutingSwitch

    tier = tier_of(tier)
    first = (providers.routing.agents.get(tier) or [None])[0]
    if first is None:
        return
    usage, rules = current_usage(), active_rules(providers.routing)
    reason = pick_reason(choice.provider, usage, rules, choice.model)
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
        self, name: str, *, model: str = "", is_pick: bool = False, remote: bool = False
    ) -> tuple[str, str]:
        provider = self.providers.providers.get(name)
        if provider is None:
            return "unavailable", f"{name} isn't in providers.yaml"
        if not remote and not provider.available:
            return "unavailable", _unavailable(provider)
        why = why_over(name, self.usage, self.rules, model)
        if is_pick:
            return "pick", pick_reason(name, self.usage, self.rules, model)
        return (
            ("skip", why)
            if why
            else ("ok", pick_reason(name, self.usage, self.rules, model))
        )


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
                choice.provider,
                model=choice.model,
                is_pick=choice == picked,
                remote=True,
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
        **window_pace({"period": window_period(name), **seen, "used": used}, now=clock),
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
        "capacity": capacity_report(providers),
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
