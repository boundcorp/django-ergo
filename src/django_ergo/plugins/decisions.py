"""Decisions plugin (experimental): quick OpenAI Decisions API calls before
a bot replies.

    plugins:
      - name: decisions
        api_key_env: OPENAI_API_KEY   # the default
        model: gpt-6-luna             # the only Decisions model so far
        min_confidence: 0.4           # below this, the chat's own tier is used
        step_down_confidence: 0.8     # a tier below the chat's needs this much
        enforce_limits: false         # true: never offer a tier whose pick is over a limit
        context_chars: 1500           # how much of the bot's previous reply to include
        tiers:                        # optional: the tiers to offer, with what each is for
          small: Quick or routine messages, acknowledgements and status checks
          large: Hard reasoning, design and large code changes
        instructions: |               # optional, added to the router's instructions
          Anything about menus or recipes is easy.

The Decisions API (``POST /v1/decisions``) answers typed questions about an
input (a predicate, a choice from a list, or a score) about ten times faster
than a model reply, and bills input tokens only.

The first decision is a tier router. For a chat on ``auto/<tier>`` it asks,
before each turn, which tier (small, medium, large, xlarge or a custom one
in providers.yaml) the
message needs, given the bot's previous reply, each tier's models, each
subscription's usage and limits and the deployment's routing priorities
(routing.md, or the text saved on Ergonaut's Routing page). The routing
rules then pick the model within that tier as usual (``bots.routing``).
The chat's own tier is the default: the router is told it's the usual
one, and it's kept unless the router is confident, more so for a step down
(``step_down_confidence``) than up (``min_confidence``). A switch is logged on
the Routing page like any other, and the turn's structured call keeps the
decision under ``metadata["routing_pick"]``. Chats on a fixed model are left
alone. When the API can't be reached or refuses, the routing rules pick as
usual, so the plugin never fails a turn.

Other code can ask its own questions with ``plugin.decide(input, questions)``.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from django_ergo.bots.plugins import BotPlugin
from django_ergo.bots.routing import WINDOW_NAMES
from django_ergo.bots.routing import RoutePick
from django_ergo.bots.routing import limit_of

if TYPE_CHECKING:
    from django_ergo.bots.routing import RouteRequest
    from django_ergo.conversation.models import ConversationSession

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_TIMEOUT = 10.0
MAX_INPUT_CHARS = 8000

TIER_PURPOSES = {
    "small": "quick or routine messages: acknowledgements, short questions, "
    "status checks, relaying a message",
    "medium": "everyday work: multi-step tasks with tools, writing, code "
    "review and ordinary code changes",
    "large": "hard work: deep reasoning, design decisions, tricky or large "
    "code changes",
    "xlarge": "the hardest and longest work: long autonomous runs and the "
    "most difficult problems",
}

ROUTER_INSTRUCTIONS = """\
Pick the tier of model that should answer the newest message in this chat.
{bot}The chat's usual tier is "{tier}"; pick another only when the message
clearly needs more or less. Higher tiers are more capable but cost more and
use up the subscriptions faster.

Judge a message by the work its answer takes, not by its length: "merge
it", "continue" or "go ahead" can start a long turn of tool calls and code
changes, so read them with the bot's previous reply when it's given. Pick a
lower tier only for a message whose answer is short and needs no tools or
code: an acknowledgement, a quick fact, a status the bot already knows. A
message in [brackets] is from the system or another bot; judge it by the
work it asks for too.

Switching models drops the chat's prompt cache, so stay on the tier of the
chat's current model unless the message needs more or less. Follow the
deployment's routing priorities and keep each subscription under its
limits: a tier whose models are all over a limit is a last resort.

Routing priorities:
{priorities}

Account usage now:
{usage}
"""


class DecisionsError(RuntimeError):
    pass


@dataclass
class Decided:
    """A Decisions API call's answers, by question name."""

    answers: dict[str, dict]
    input_tokens: int = 0


class DecisionsPlugin(BotPlugin):
    name = "decisions"

    def on_load(self) -> None:
        self.api_key_env = str(self.config.get("api_key_env") or "OPENAI_API_KEY")
        self.model = str(self.config.get("model") or DEFAULT_MODEL)
        self.base_url = self.config.get("base_url") or None
        self.timeout = float(self.config.get("timeout") or DEFAULT_TIMEOUT)
        self.min_confidence = float(self.config.get("min_confidence", 0.4))
        self.step_down_confidence = float(self.config.get("step_down_confidence", 0.8))
        self.enforce_limits = bool(self.config.get("enforce_limits", False))
        self.context_chars = int(self.config.get("context_chars", 1500))
        given = self.config.get("tiers") or {}  # empty: every tier
        if isinstance(given, list):
            given = dict.fromkeys(given, "")
        self.tiers = {str(t): str(p or "") for t, p in given.items()}
        self.instructions = str(self.config.get("instructions") or "").strip()
        self._client = None

    # -- the API -----------------------------------------------------------

    @property
    def client(self):
        if self._client is None:
            key = os.environ.get(self.api_key_env)
            if not key:
                msg = f"{self.api_key_env} isn't set"
                raise DecisionsError(msg)
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=key,
                base_url=self.base_url,
                timeout=self.timeout,
                max_retries=0,
            )
        return self._client

    async def decide(self, input: Any, questions: list[dict]) -> Decided:  # noqa: A002
        """Ask ``questions`` about ``input`` (text, or user messages with
        ``input_text`` and ``input_image`` parts)."""
        client = self.client
        if not hasattr(client, "decisions"):
            msg = "The Decisions API needs the openai package 3.26 or later"
            raise DecisionsError(msg)
        decision = await client.decisions.create(
            model=self.model, input=input, questions=questions
        )
        answers = {}
        # Answers come in question order; a name is optional.
        for question, answer in zip(questions, decision.answers, strict=False):
            data = answer.model_dump()
            answers[data.get("name") or question["name"]] = data
        usage = decision.usage.model_dump() if decision.usage else {}
        return Decided(answers=answers, input_tokens=usage.get("input_tokens", 0))

    # -- the model router ----------------------------------------------------

    async def route_turn(
        self, session: ConversationSession, message: str, request: RouteRequest
    ) -> RoutePick | None:
        choices = self.router_choices(request)
        if len(choices) < 2 or not message.strip():  # noqa: PLR2004 - nothing to pick
            return None
        question = {
            "type": "choice",
            "name": "tier",
            "instructions": self.router_instructions(request),
            "choices": choices,
        }
        started = time.monotonic()
        try:
            text = await self.router_input(session, message)
            decided = await self.decide(text, [question])
        except Exception:  # noqa: BLE001 - the routing rules take over
            logger.warning(
                "%s: tier router decision failed", self.bot.name, exc_info=True
            )
            return None
        latency_ms = round((time.monotonic() - started) * 1000)
        answer = decided.answers.get("tier")
        if not answer or answer.get("type") != "choice":
            logger.info("%s: tier router gave no choice: %r", self.bot.name, answer)
            return None
        confidence = float(answer.get("confidence") or 0)
        tier = answer.get("choice")
        offered = [c["value"] for c in choices]
        if tier not in offered:
            return None
        details = {
            "source": "decisions",
            "picked_tier": tier,
            "confidence": confidence,
            "probabilities": answer.get("probabilities") or [],
            "tiers": offered,
            "latency_ms": latency_ms,
            "input_tokens": decided.input_tokens,
            "decision_model": self.model,
        }
        order = list(request.tiers)
        lower = request.tier in order and order.index(tier) < order.index(request.tier)
        needed = self.step_down_confidence if lower else self.min_confidence
        if confidence < needed:
            if request.tier not in request.tiers:
                return None
            tier = request.tier
            reason = (
                f"Decisions router unsure ({confidence:.0%}); the chat's {tier} tier"
            )
        else:
            reason = (
                f"Decisions router picked the {tier} tier ({confidence:.0%} confident)"
            )
        return RoutePick(
            model=request.pick(tier), tier=tier, reason=reason, details=details
        )

    async def router_input(self, session: ConversationSession, message: str) -> str:
        """The message, after the bot's previous reply when there is one."""
        message = message[:MAX_INPUT_CHARS]
        if self.context_chars <= 0:
            return message
        previous = await self._previous_reply(session)
        if not previous:
            return message
        if len(previous) > self.context_chars:
            previous = "…" + previous[-self.context_chars :]
        return f"The bot's previous reply:\n{previous}\n\nNew message:\n{message}"

    async def _previous_reply(self, session: ConversationSession) -> str:
        from django_ergo.conversation.chat_reply import CHAT_REPLY_KIND
        from django_ergo.conversation.models import StructuredCall

        call = (
            await StructuredCall.objects.filter(
                session=session, kind=CHAT_REPLY_KIND, response__isnull=False
            )
            .order_by("-created_at")
            .afirst()
        )
        response = call.response if call is not None else None
        return str(response.get("text") or "") if isinstance(response, dict) else ""

    def router_choices(self, request: RouteRequest) -> list[dict]:
        """The tiers as Decisions choices: what each is for, its models, and
        the model the rules would use in it."""
        tiers = [t for t in request.tiers if not self.tiers or t in self.tiers]
        if self.enforce_limits:
            under = [t for t in tiers if not request.why_over(request.pick(t))]
            tiers = under or tiers
        return [
            {"value": tier, "description": self._describe(tier, request)}
            for tier in tiers
        ]

    def _describe(self, tier: str, request: RouteRequest) -> str:
        pick = request.pick(tier)
        models = ", ".join(request.label(ref) for ref in request.tiers[tier])
        purpose = self.tiers.get(tier) or TIER_PURPOSES.get(tier, "")
        parts = [f"For {purpose}." if purpose else f"The {tier} tier."]
        parts.append(f"Models: {models}.")
        parts.append(f"Would use {request.label(pick)} now.")
        if why := request.why_over(pick):
            parts.append(f"Over its limit: {why}.")
        if tier == request.tier:
            parts.append("The chat's usual tier.")
        if request.current in request.tiers[tier]:
            parts.append("The chat's current model is in this tier.")
        return " ".join(parts)

    def router_instructions(self, request: RouteRequest) -> str:
        about = self.bot.definition.description
        text = ROUTER_INSTRUCTIONS.format(
            bot=f"The bot is {self.bot.name}: {about}.\n" if about else "",
            tier=request.tier,
            priorities=request.text or "(none written)",
            usage=self._usage(request),
        )
        if self.instructions:
            text += f"\n{self.instructions}\n"
        return text

    def _usage(self, request: RouteRequest) -> str:
        lines = []
        for name in dict.fromkeys(ref.partition("/")[0] for ref in request.candidates):
            used = request.usage.get(name) or {}
            windows = [
                f"{WINDOW_NAMES.get(w, w.replace('_', ' '))} {pct:.0f}% used "
                f"(limit {limit_of(name, w, request.rules):g}%)"
                for w, pct in used.items()
            ]
            lines.append(f"- {name}: {', '.join(windows) or 'no usage reported'}")
        return "\n".join(lines)
