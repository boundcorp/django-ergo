"""Decisions plugin (experimental): quick OpenAI Decisions API calls before
a bot replies.

    plugins:
      - name: decisions
        api_key_env: OPENAI_API_KEY   # the default
        model: gpt-6-luna             # the only Decisions model so far
        min_confidence: 0.4           # below this, the routing rules decide
        enforce_limits: false         # true: never offer a model over its limit
        instructions: |               # optional, added to the router's instructions
          Anything about menus or recipes is easy; use the cheapest model.

The Decisions API (``POST /v1/decisions``) answers typed questions about an
input (a predicate, a choice from a list, or a score) about ten times faster
than a model reply, and bills input tokens only.

The first decision is a model router. For a chat on ``auto/<tier>`` it asks,
before each turn, which of the tier's models should answer the user's
message, given each subscription's usage and limits and the deployment's
routing priorities (routing.md, or the text saved on Ergonaut's Routing
page). Its pick replaces the rule-based one (``bots.routing``); a switch is
logged on the Routing page like any other, and the turn's structured call
keeps the decision under ``metadata["routing_pick"]``. Chats on a fixed
model are left alone. When the API can't be reached, refuses, or isn't
confident enough, the routing rules pick as usual, so the plugin never fails
a turn.

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
from django_ergo.bots.routing import WINDOWS
from django_ergo.bots.routing import RoutePick
from django_ergo.bots.routing import limit_of

if TYPE_CHECKING:
    from django_ergo.bots.routing import RouteRequest
    from django_ergo.conversation.models import ConversationSession

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_TIMEOUT = 10.0
MAX_INPUT_CHARS = 8000

ROUTER_INSTRUCTIONS = """\
Pick the model that should answer the user's message, in a chat on the
"{tier}" tier. The choices are listed in order of preference.

- Match the model to what the message needs: a quick or simple ask can go
  to a cheaper, faster model; hard reasoning, code or long work to a
  stronger one.
- Follow the deployment's routing priorities and keep each subscription
  under its limits; use a model that is over a limit only when every
  choice is.
- Staying on the chat's current model keeps its prompt cache, so switch
  only for a reason.

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
        self.enforce_limits = bool(self.config.get("enforce_limits", False))
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
            "name": "model",
            "instructions": self.router_instructions(request),
            "choices": choices,
        }
        started = time.monotonic()
        try:
            decided = await self.decide(message[:MAX_INPUT_CHARS], [question])
        except Exception:  # noqa: BLE001 - the routing rules take over
            logger.warning(
                "%s: model router decision failed", self.bot.name, exc_info=True
            )
            return None
        latency_ms = round((time.monotonic() - started) * 1000)
        answer = decided.answers.get("model")
        if not answer or answer.get("type") != "choice":
            logger.info("%s: model router gave no choice: %r", self.bot.name, answer)
            return None
        model = answer.get("choice")
        confidence = float(answer.get("confidence") or 0)
        values = [c["value"] for c in choices]
        if model not in values or confidence < self.min_confidence:
            logger.info(
                "%s: model router picked %r at %.2f; using the routing rules",
                self.bot.name,
                model,
                confidence,
            )
            return None
        return RoutePick(
            model=model,
            reason=f"Decisions router picked it ({confidence:.0%} confident)",
            details={
                "source": "decisions",
                "confidence": confidence,
                "probabilities": answer.get("probabilities") or [],
                "candidates": values,
                "latency_ms": latency_ms,
                "input_tokens": decided.input_tokens,
                "decision_model": self.model,
            },
        )

    def router_choices(self, request: RouteRequest) -> list[dict]:
        """The tier's models as Decisions choices, each with its provider's
        usage and whether it's over a limit."""
        candidates = list(request.candidates)
        if self.enforce_limits:
            under = [ref for ref in candidates if not request.why_over(ref)]
            candidates = under or candidates
        return [
            {"value": ref, "description": self._describe(ref, request)}
            for ref in candidates
        ]

    def _describe(self, ref: str, request: RouteRequest) -> str:
        found = request.providers.find(ref)
        provider = found[0] if found else None
        billing = (
            "on a subscription"
            if provider is not None and provider.transport == "cli"
            else "on an API key, billed per token"
        )
        parts = [f"{request.label(ref)} ({ref.partition('/')[0]}, {billing})."]
        if ref == request.current:
            parts.append("The chat's current model.")
        if why := request.why_over(ref):
            parts.append(f"Over its limit: {why}.")
        return " ".join(parts)

    def router_instructions(self, request: RouteRequest) -> str:
        text = ROUTER_INSTRUCTIONS.format(
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
                f"{WINDOW_NAMES[w]} {used[w]:.0f}% used "
                f"(limit {limit_of(name, w, request.rules):g}%)"
                for w in WINDOWS
                if w in used
            ]
            lines.append(f"- {name}: {', '.join(windows) or 'no usage reported'}")
        return "\n".join(lines)
