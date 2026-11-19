"""Opt-in Hindsight working memory. See docs/hindsight-memory.md."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from urllib.parse import quote

import httpx
from django.conf import settings
from django.utils import timezone
from django.utils.module_loading import import_string

from django_ergo.bots.plugins import BotPlugin
from django_ergo.bots.tools import FunctionToolkit
from django_ergo.bots.tools import bot_tool
from django_ergo.conversation.context import TextContextSource
from django_ergo.settings import api_settings

logger = logging.getLogger(__name__)
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hindsight-retain")
_recall_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hindsight-recall")
PREFETCH_MODES = {"new_session", "every_turn", "off"}
RETAIN_MODES = {"after_turn", "off"}
BUDGETS = {"low", "mid", "high"}


def _service() -> tuple[str, str] | None:
    url = getattr(settings, "HINDSIGHT_URL", "").strip().rstrip("/")
    key = getattr(settings, "HINDSIGHT_API_KEY", "").strip()
    return (url, key) if url else None


def _post(bank: str, path: str, payload: dict, timeout: float) -> dict:
    url, key = _service() or ("", "")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    response = httpx.post(
        f"{url}/v1/default/banks/{quote(bank, safe='')}/{path}",
        json=payload,
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def retain_item(bank: str, item: dict, timeout: float = 30) -> None:
    """Worker entry point. Hindsight processes the item after the HTTP response."""
    try:
        _post(bank, "memories", {"items": [item], "async": True}, timeout)
    except Exception:  # memory failures cannot break a turn
        logger.exception("Hindsight retain failed for bank %s", bank)


def _enqueue(bank: str, item: dict) -> None:
    try:
        runner = getattr(api_settings, "HINDSIGHT_RETAIN_RUNNER", None)
        if runner:
            if isinstance(runner, str):
                runner = import_string(runner)
            runner(bank, item)
        else:
            retain_item(bank, item)
    except Exception:  # broker failure is best effort
        logger.exception("Could not queue Hindsight retain for bank %s", bank)


class HindsightMemoryPlugin(BotPlugin):
    name = "hindsight_memory"
    description = "Recall or save long-term Hindsight memory; reflect is slower"

    @property
    def skill_name(self) -> str:
        return "hindsight_memory"

    @property
    def skill_instructions(self) -> str:
        return "Use memory_recall for targeted facts, memory_reflect for slower synthesis, and memory_retain when explicitly asked to remember something."

    def on_load(self) -> None:
        self.bank = str(self.config.get("bank", "ergonaut"))
        self.retain = self.config.get("retain", "after_turn")
        self.prefetch = self.config.get("prefetch", "every_turn")
        if self.retain is False:
            self.retain = "off"
        if self.prefetch is False:
            self.prefetch = "off"
        if self.retain not in RETAIN_MODES or self.prefetch not in PREFETCH_MODES:
            msg = "Invalid hindsight_memory retain or prefetch mode"
            raise ValueError(msg)
        self.top_k = max(1, int(self.config.get("top_k", 5)))
        self.budget = self.config.get("budget", "low")
        if self.budget not in BUDGETS:
            msg = "hindsight_memory budget must be low, mid or high"
            raise ValueError(msg)
        self.timeout = max(0.1, float(self.config.get("timeout_seconds", 3)))
        self.enabled = _service() is not None
        if not self.enabled:
            logger.warning("Hindsight memory disabled: HINDSIGHT_URL is unset")

    def _bank(self, ctx) -> str:
        # Config owns bank selection. A later per-bot/user template can use
        # this method without adding bank to any model-visible tool schema.
        return self.bank

    def _tags(self, session) -> list[str]:
        tags = [f"bot:{self.bot.name}", f"chat:{session.id}", f"user:{session.user_id}"]
        if session.parent_id:
            tags.append(f"thread:{session.id}")
        return tags

    def _item(self, session, content: str, *, document_id: str | None = None) -> dict:
        tags = self._tags(session)
        item = {
            "content": content,
            "timestamp": timezone.now().isoformat(),
            "context": "Ergonaut conversation",
            "tags": tags,
            "metadata": {tag.split(":", 1)[0]: tag.split(":", 1)[1] for tag in tags},
        }
        if document_id:
            item["document_id"] = document_id
        return item

    def _retain(self, session, content: str, *, document_id: str | None = None) -> None:
        if self.enabled and content.strip():
            try:
                _pool.submit(
                    _enqueue,
                    self._bank(None),
                    self._item(session, content, document_id=document_id),
                )
            except Exception:
                logger.exception(
                    "Could not schedule Hindsight retain for bank %s", self.bank
                )

    def _scope(self, session) -> dict:
        return {"tags": [f"user:{session.user_id}"], "tags_match": "any_strict"}

    def _recall(self, session, query: str, *, timeout: float) -> str:
        data = _post(
            self.bank,
            "memories/recall",
            {
                "query": query,
                "budget": self.budget,
                "max_tokens": 1000,
                **self._scope(session),
            },
            timeout,
        )
        return "\n".join(
            f"- {item['text']}"
            for item in data.get("results", [])[: self.top_k]
            if item.get("text")
        )

    def make_toolkit(self, ctx):
        plugin = self

        @bot_tool(name="memory_recall")
        def recall(query: str) -> str:
            """Search long-term memory for relevant facts."""
            return plugin._recall(ctx.session, query, timeout=plugin.timeout)  # noqa: SLF001

        @bot_tool(name="memory_reflect")
        def reflect(question: str) -> str:
            """Ask Hindsight to synthesize an answer from memory. This can be slow."""
            data = _post(
                plugin._bank(ctx),  # noqa: SLF001
                "reflect",
                {
                    "query": question,
                    "budget": plugin.budget,
                    **plugin._scope(ctx.session),  # noqa: SLF001
                },
                60,
            )
            return data.get("text", "")

        @bot_tool(name="memory_retain")
        def retain(text: str) -> str:
            """Save an explicitly requested fact to long-term memory."""
            plugin._retain(ctx.session, text)  # noqa: SLF001
            return "Queued for memory."

        return FunctionToolkit([f.__bot_tool__ for f in (recall, reflect, retain)], ctx)

    def toolkits(self, ctx):
        return [self.make_toolkit(ctx)] if self.enabled else []

    def always_context_sources(self, ctx, message):
        if not self.enabled or not message.strip() or self.prefetch == "off":
            return []
        if self.prefetch == "new_session" and ctx.session.messages.exists():
            return []

        def search() -> str:
            try:
                future = _recall_pool.submit(
                    self._recall, ctx.session, message, timeout=self.timeout
                )
                return future.result(timeout=self.timeout)
            except FutureTimeout:
                logger.warning("Hindsight prefetch timed out for bank %s", self.bank)
                return ""
            except Exception:  # prefetch is best effort
                logger.exception("Hindsight prefetch failed for bank %s", self.bank)
                return ""

        return [TextContextSource("Relevant long-term memory", search)]

    async def after_turn(self, session, message, result) -> None:
        if (
            not self.enabled
            or self.retain != "after_turn"
            or not message.strip()
            or not result.text.strip()
        ):
            return
        if result.error or result.needs_approval or result.call is None:
            return
        meta = result.call.metadata or {}
        author = meta.get("message_author") or {}
        if author and author.get("kind") != "django_user":
            return
        if (
            meta.get("message_provenance")
            or meta.get("recurring_wake")
            or meta.get("periodic_checkins")
        ):
            return
        content = f"User: {message}\nAssistant: {result.text}"
        self._retain(session, content, document_id=f"ergonaut-turn-{result.call.pk}")
