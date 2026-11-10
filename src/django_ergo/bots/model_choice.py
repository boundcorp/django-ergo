"""Model choice for the tasks a bot starts: which models it may hand them, and
the one rule every model argument passes.

A bot starts sub-agents and side tasks: coding agents (``ergo_agent_start``,
``orca_start_worker``, local_shell jobs), browser tasks (``ergo_browser_task``)
and a bot's own tools. :func:`available` lists what the calling bot may pick
(the ``ergo_models_list`` tool returns it), and :func:`check` is the rule for
a model a tool was given. Two kinds of task take a model:

- ``AGENT``: a coding agent CLI on a subscription. Pick an ``agents`` tier
  from providers.yaml (routing picks agent, model and effort), or name a
  model the agent understands. Never a private model: its harness can't
  carry a private grant.
- ``MODEL``: an Ergo engine call in this process. ``auto/<tier>`` or a
  ``provider/model`` from providers.yaml.

A bot in a private pool (``allowed_bots``) keeps its work in that pool: a
``MODEL`` task may only pick ``<pool>`` or ``<pool>/<alias>``, and it can't
start coding agents at all, since none can run on a private pool.

A bot's own tools use it the same way::

    from django_ergo.bots import model_choice

    model = model_choice.check(ctx.bot, model, model_choice.MODEL) or default
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django_ergo.bots.routing import AUTO
from django_ergo.bots.routing import agent_label
from django_ergo.bots.routing import is_auto
from django_ergo.bots.routing import pick_agent
from django_ergo.bots.routing import pick_model
from django_ergo.bots.routing import tier_of
from django_ergo.conversation.inference_policy import PolicyError

if TYPE_CHECKING:
    from django_ergo.bots.runtime import Bot
    from django_ergo.bots.tools import ToolContext

AGENT = "agent"
MODEL = "model"
KINDS = (AGENT, MODEL)


def available(bot: Bot, kind: str = "") -> dict:
    """What ``bot`` may hand a task it starts, with routing's pick for each tier
    right now (runs the ORM). ``kind`` (``agent`` or ``model``) narrows it."""
    if kind and kind not in KINDS:
        msg = f"kind is {' or '.join(KINDS)}, not {kind!r}"
        raise ValueError(msg)
    if pool := bot.private_pool:
        return _pool_view(bot, pool)
    providers = bot.providers
    out: dict = {}
    if kind in ("", AGENT):
        out["agent_tiers"] = [
            _agent_tier(providers, tier, choices)
            for tier, choices in providers.routing.agents.items()
            if choices
        ]
    if kind in ("", MODEL):
        out["model_tiers"] = [
            _model_tier(providers, tier, refs)
            for tier, refs in providers.routing.tiers.items()
            if refs
        ]
        out["models"] = [
            {
                "model": m.id,
                "label": m.label or m.name,
                "available": providers.providers[m.provider].available,
            }
            for m in providers.models()
        ]
        if default := bot.model_ref():
            out["default"] = default
    out["how_to_pick"] = (
        "Pass a tier unless the task needs one model: routing then picks the "
        "subscription with the most headroom. Size the tier to the work: small "
        "for lookups and mechanical edits, medium for ordinary tasks, large or "
        "xlarge for hard design or debugging. Coding agents take an agent tier "
        "(tier=...); model tasks take auto/<tier> or a listed model (model=...)."
    )
    return out


def _agent_tier(providers, tier: str, choices) -> dict:
    try:
        pick = agent_label(pick_agent(providers, tier))
    except ValueError as exc:
        pick = f"none now ({exc})"
    return {
        "tier": tier,
        "pick_now": pick,
        "candidates": [agent_label(c) for c in choices],
    }


def _model_tier(providers, tier: str, refs: list[str]) -> dict:
    try:
        pick = pick_model(providers, tier)
    except ValueError as exc:
        pick = f"none now ({exc})"
    return {"model": f"{AUTO}{tier}", "pick_now": pick, "candidates": list(refs)}


def _pool_view(bot: Bot, pool) -> dict:
    out = {
        "private_pool": pool.name,
        "models": [
            {
                "model": f"{pool.name}/{alias}",
                "default": alias == pool.default,
            }
            for alias in pool.models
        ],
        "agent_tiers": [],
        "how_to_pick": (
            f"{bot.name} keeps its work in the private pool {pool.name!r}: a model "
            f"task takes {pool.name} (its default) or {pool.name}/<alias>, and "
            "coding agents can't run here, because none can use a private pool."
        ),
    }
    if not pool.enabled:
        out["disabled"] = True
    return out


def check(bot: Bot, model: str, kind: str) -> str:
    """``model`` (stripped) if ``bot`` may hand it to a ``kind`` task, else
    PolicyError (a private model where it can't go) or ValueError (unknown).
    An empty model is allowed: the tool's default applies."""
    if kind not in KINDS:
        msg = f"kind is {' or '.join(KINDS)}, not {kind!r}"
        raise ValueError(msg)
    model = str(model or "").strip()
    return _check_agent(bot, model) if kind == AGENT else _check_model(bot, model)


def _check_agent(bot: Bot, model: str) -> str:
    if pool := bot.private_pool:
        msg = (
            f"{bot.name} is in the private pool {pool.name!r}, and coding agents "
            "can't run on a private pool, so it can't start them"
        )
        raise PolicyError(msg)
    if bot.providers.is_private(model):
        msg = (
            f"Coding agents can't use the private model {model!r}: private pools "
            "are for allowlisted bots' own chats only"
        )
        raise PolicyError(msg)
    # Agent CLIs take their own model names (omp: provider/model).
    return model


def _check_model(bot: Bot, model: str) -> str:
    if not model:
        return model
    if pool := bot.private_pool:
        head, _, alias = model.partition("/")
        if head != pool.name:
            members = ", ".join(f"{pool.name}/{a}" for a in pool.models)
            msg = (
                f"{bot.name} keeps its work in the private pool {pool.name!r}: pick "
                f"{pool.name} or one of {members}, not {model!r}"
            )
            raise PolicyError(msg)
        pool.resolve(alias)  # PolicyError for a member it doesn't have
        return model
    providers = bot.providers
    providers.check_public(model, where=bot.name)
    if is_auto(model):
        tier = tier_of(model)
        if tier not in providers.routing.tiers:
            known = ", ".join(f"{AUTO}{t}" for t in providers.routing.tiers) or "none"
            msg = f"No model tier {tier!r} in providers.yaml (tiers: {known})"
            raise ValueError(msg)
    elif providers and providers.find(model) is None:
        known = ", ".join(m.id for m in providers.models()) or "none"
        msg = f"Unknown model {model!r}; providers.yaml lists {known}"
        raise ValueError(msg)
    return model


MODELS_INSTRUCTIONS = """\
Before starting a sub-agent or task that takes a model (a coding agent, a
browser task, a bot tool with a model argument), ergo_models_list shows what
this bot may pick and what routing would pick for each tier now. Prefer a tier
over a pinned model; pin one only when the task needs it.
"""


def models_toolkit(bot: Bot, ctx: ToolContext):
    """The ``models`` skill: ``ergo_models_list``."""
    from django_ergo.bots.tools import FunctionToolkit
    from django_ergo.bots.tools import bot_tool

    @bot_tool(
        name="ergo_models_list",
        takes_context=True,
        description=(
            "The model tiers and models this bot may hand a task it starts (coding "
            "agents, browser tasks, tools with a model argument), and what routing "
            "picks for each tier now."
        ),
        parameters={
            "kind": {
                "type": "string",
                "enum": list(KINDS),
                "description": "agent: coding agent tiers; model: Ergo model tiers "
                "and models. Both when left out.",
            }
        },
    )
    def list_models(ctx: ToolContext, kind: str = "") -> dict:
        return available(ctx.bot, kind)

    return FunctionToolkit([list_models.__bot_tool__], ctx)
