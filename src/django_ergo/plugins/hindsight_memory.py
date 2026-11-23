"""Opt-in Hindsight working memory. See docs/hindsight-memory.md."""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait
from urllib.parse import quote

import httpx
from asgiref.sync import sync_to_async
from django.conf import settings
from django.utils import timezone
from django.utils.module_loading import import_string

from django_ergo import signals
from django_ergo.bots.plugins import BotPlugin
from django_ergo.bots.tools import FunctionToolkit
from django_ergo.bots.tools import bot_tool
from django_ergo.conversation.context import ContextSection
from django_ergo.conversation.context import ContextSource
from django_ergo.conversation.context import estimate_tokens
from django_ergo.settings import api_settings

logger = logging.getLogger(__name__)
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hindsight-retain")
_recall_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="hindsight-recall")
PREFETCH_MODES = {"new_session", "every_turn", "off"}
RETAIN_MODES = {"after_turn", "off"}
BUDGETS = {"low", "mid", "high"}
SCOPES = {"bot_first", "user"}
TITLE = "Relevant long-term memory"
MODELS_TITLE = "Working memory"
MODELS_TTL = 600
# Reranker floors, tuned on the audit's probe set (docs/hindsight-memory.md).
DEFAULT_MIN_SCORE = 0.5
DEFAULT_MIN_SCORE_SHARED = 0.8
DUPLICATE_SIMILARITY = 0.7

# Tools that only read the bot's own config, skills or docs.
CONFIG_READ_TOOLS = frozenset(
    {
        "ergo_self_read",
        "ergo_self_files",
        "ergo_skill_load",
        "ergo_config_repo_status",
        "ergo_config_repo_list",
        "ergo_config_repo_read",
        "ergo_config_repo_grep",
        "ergo_config_repo_diff",
    }
)
CHAT_CONTEXT = (
    "Ergonaut chat with the bot '{bot}'. That bot's configuration and docs "
    "(bot.yaml, agents.md, docs/, skills, ENVIRONMENT.md) are authoritative for "
    "its rules, policies, tools, tiers, hosts and procedures. Keep only the "
    "user's decisions, preferences, outcomes, incidents and app or project "
    "state. Never keep assistant offers, plans, check-in times, routing or "
    "relay narration, or restatements of rules or config."
)
_MENU_HEADING = re.compile(
    r"^\W*(suggested replies|suggested responses|suggestions|quick replies|options|choices)\b.*$",
    re.IGNORECASE,
)
_STATUS_LINE = re.compile(
    r"^\W*(status|progress|next check(?:[- ]?in)?|check[- ]?in|eta|next update)\s*[:\-\u2013\u2014].*$",
    re.IGNORECASE,
)
_LIST_ITEM = re.compile(r"^\s*(?:[-*•]|\d+[.)]|\(?[A-Za-z][.)])\s+\S")
_OFFER = re.compile(
    r"^\W*(want me to|would you like|should i|shall i|do you want me to|let me know)\b",
    re.IGNORECASE,
)
_DECISION = re.compile(
    r"\b(yes|yep|yeah|ok|okay|approve[ds]?|go ahead|do it|decid\w*|prefer\w*|always|never|"
    r"from now on|going forward|remember|don'?t|do not|stop|switch|instead|"
    r"let'?s|i want|i'd like|ship it|merge|use)\b",
    re.IGNORECASE,
)
_WORDS = re.compile(r"[a-z0-9]+")
_models_cache: dict[tuple[str, str], tuple[float, str]] = {}
_models_lock = threading.Lock()


def _service() -> tuple[str, str] | None:
    url = getattr(settings, "HINDSIGHT_URL", "").strip().rstrip("/")
    key = getattr(settings, "HINDSIGHT_API_KEY", "").strip()
    return (url, key) if url else None


def _request(method: str, bank: str, path: str, payload, timeout: float) -> dict:
    url, key = _service() or ("", "")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    target = f"{url}/v1/default/banks/{quote(bank, safe='')}/{path}"
    if method == "GET":
        response = httpx.get(target, headers=headers, timeout=timeout)
    else:
        response = httpx.post(target, json=payload, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _post(bank: str, path: str, payload: dict, timeout: float) -> dict:
    return _request("POST", bank, path, payload, timeout)


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


# -- retain pre-filter ---------------------------------------------------------


def clean_assistant_text(text: str) -> str:  # noqa: C901
    """The assistant's reply without suggestion menus, status lines and offers.

    Deterministic and conservative: it only removes things that are never
    durable facts. Hindsight's extractor sees the rest.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if _MENU_HEADING.match(line.strip()):
            lines = lines[:index]
            break
    lines = [line for line in lines if not _STATUS_LINE.match(line.strip())]
    # A trailing option list: list items under a closing question.
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    start = end
    while start and _LIST_ITEM.match(lines[start - 1]):
        start -= 1
    if end - start >= 2 and start:  # noqa: PLR2004
        lead = start - 1
        while lead and not lines[lead].strip():
            lead -= 1
        if lines[lead].rstrip().endswith("?"):
            lines = lines[:lead]
            end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    lines = lines[:end]
    # A closing offer ("Want me to ...?") in the last paragraph.
    while lines and _OFFER.match(lines[-1].strip()):
        lines.pop()
        while lines and not lines[-1].strip():
            lines.pop()
    return "\n".join(lines).strip()


def machine_turn(meta: dict | None, telegram_users=()) -> str:
    """Why a turn was not written by a person, or "" for a human turn."""
    meta = meta or {}
    author = meta.get("message_author") or {}
    kind = author.get("kind")
    if kind == "telegram_user":
        if str(author.get("ref")) not in telegram_users:
            return "telegram_unmapped"
    elif author and kind != "django_user":
        return "author"
    if meta.get("message_provenance"):
        return "provenance"
    if meta.get("recurring_wake") or meta.get("periodic_checkins"):
        return "recurring_wake"
    return ""


def chat_tags(bot: str, session, source: str = "chat") -> list[str]:
    """The tags every memory of this chat carries (recall scopes on them)."""
    tags = [f"bot:{bot}", f"chat:{session.id}", f"user:{session.user_id}"]
    if session.parent_id:
        tags.append(f"thread:{session.id}")
    tags.append(f"source:{source}")
    return tags


def has_decision(message: str) -> bool:
    """Whether a user message looks like it carries a decision or preference."""
    return bool(_DECISION.search(message))


def _tokens(text: str) -> set[str]:
    return set(_WORDS.findall(text.lower()))


def _similar(a: str, b: str) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return a.strip().lower() == b.strip().lower()
    return len(ta & tb) / len(ta | tb) >= DUPLICATE_SIMILARITY or ta <= tb or tb <= ta


def _merge(first: list[dict], shared: list[dict], limit: int) -> list[dict]:
    """Bot-scoped hits first, then user-wide ones, without near-duplicates."""
    chosen: list[dict] = []
    for hit in [*first, *shared]:
        if len(chosen) >= limit:
            break
        if any(_similar(hit["text"], other["text"]) for other in chosen):
            continue
        chosen.append(hit)
    return chosen


class MemorySource(ContextSource):
    """The prefetch block. Empty when nothing clears the floors."""

    def __init__(self, plugin: HindsightMemoryPlugin, session, message: str, bot: str):
        self.plugin = plugin
        self.session = session
        self.message = message
        self.bot = bot
        self.weight = 1.0

    def render(self, budget_tokens: int) -> ContextSection | None:
        found = self.plugin.prefetch_block(self.session, self.message, self.bot)
        if not found["text"]:
            return None
        text = found["text"]
        if estimate_tokens(text) > budget_tokens:
            text = text[: max(budget_tokens * 4 - 12, 0)].rstrip() + " [truncated]"
        return ContextSection(
            TITLE,
            text,
            estimate_tokens(text),
            details={"hindsight": found["details"]},
        )


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
        self.min_score = float(self.config.get("min_score", DEFAULT_MIN_SCORE))
        self.min_score_shared = float(
            self.config.get("min_score_shared", DEFAULT_MIN_SCORE_SHARED)
        )
        for floor in (self.min_score, self.min_score_shared):
            if not 0 <= floor <= 1:
                msg = "hindsight_memory min_score must be between 0 and 1"
                raise ValueError(msg)
        self.scope = self.config.get("scope", "bot_first")
        if self.scope not in SCOPES:
            msg = "hindsight_memory scope must be bot_first or user"
            raise ValueError(msg)
        self.telegram_users = {str(u) for u in self.config.get("telegram_users") or []}
        self.mental_models = [str(m) for m in self.config.get("mental_models") or []]
        self.top_k_with_models = max(
            1, int(self.config.get("top_k_with_models", min(3, self.top_k)))
        )
        from django_ergo.plugins.hindsight_pr_events import PrEventsConfig

        self.pr_events = PrEventsConfig(
            self.config.get("pr_events") or {},
            list(getattr(getattr(self.bot, "definition", None), "pull_requests", [])),
        )
        self.enabled = _service() is not None
        if not self.enabled:
            logger.warning("Hindsight memory disabled: HINDSIGHT_URL is unset")

    def _bank(self, ctx) -> str:
        # Config owns bank selection. A later per-bot/user template can use
        # this method without adding bank to any model-visible tool schema.
        return self.bank

    def _emit(self, event: str, outcome: str, **fields) -> None:
        try:
            signals.memory_event.send_robust(
                sender=type(self),
                plugin=self.name,
                bot_name=getattr(self.bot, "name", ""),
                event=event,
                outcome=outcome,
                reason=fields.pop("reason", ""),
                duration=fields.pop("duration", 0.0),
                count=fields.pop("count", 0),
            )
        except Exception:  # noqa: BLE001 - observation never breaks a turn
            logger.debug("memory_event receiver failed", exc_info=True)

    # -- who wrote the turn ----------------------------------------------------

    def machine_turn(self, meta: dict | None) -> str:
        """Why this turn was not written by a person, or "" for a human turn."""
        return machine_turn(meta, self.telegram_users)

    # -- tags and items --------------------------------------------------------

    def _tags(self, session, source: str = "chat") -> list[str]:
        return chat_tags(self.bot.name, session, source)

    def _item(
        self,
        session,
        content: str,
        *,
        document_id: str | None = None,
        source: str = "chat",
    ) -> dict:
        tags = self._tags(session, source)
        item = {
            "content": content,
            "timestamp": timezone.now().isoformat(),
            "context": CHAT_CONTEXT.format(bot=self.bot.name),
            "tags": tags,
            "metadata": {tag.split(":", 1)[0]: tag.split(":", 1)[1] for tag in tags},
        }
        if document_id:
            item["document_id"] = document_id
        return item

    def _retain(
        self,
        session,
        content: str,
        *,
        document_id: str | None = None,
        source: str = "chat",
    ) -> None:
        if self.enabled and content.strip():
            try:
                _pool.submit(
                    _enqueue,
                    self._bank(None),
                    self._item(
                        session, content, document_id=document_id, source=source
                    ),
                )
                self._emit("retain", "queued")
            except Exception:
                logger.exception(
                    "Could not schedule Hindsight retain for bank %s", self.bank
                )

    # -- recall ----------------------------------------------------------------

    def _scope(self, session) -> dict:
        return {"tags": [f"user:{session.user_id}"], "tags_match": "any_strict"}

    def _bot_scope(self, session) -> dict:
        return {
            "tag_groups": [
                {
                    "and": [
                        {"tags": [f"bot:{self.bot.name}"], "match": "any_strict"},
                        {"tags": [f"user:{session.user_id}"], "match": "any_strict"},
                    ]
                }
            ]
        }

    def _recall_payload(self, query: str, scope: dict, floor: float | None) -> dict:
        payload = {
            "query": query,
            "budget": self.budget,
            "max_tokens": 1000,
            "prefer_observations": True,
            "query_timestamp": timezone.now().strftime("%Y-%m-%dT%H:%M:%S"),
            **scope,
        }
        if floor:
            payload["min_scores"] = {"reranker": floor}
        return payload

    def _recall_hits(
        self, query: str, scope: dict, floor: float | None, timeout: float, label: str
    ) -> list[dict]:
        data = _post(
            self.bank,
            "memories/recall",
            self._recall_payload(query, scope, floor),
            timeout,
        )
        return [
            {"id": str(item.get("id") or ""), "text": item["text"], "scope": label}
            for item in data.get("results", [])
            if item.get("text")
        ]

    def _recall(self, session, query: str, *, timeout: float) -> str:
        """The explicit memory_recall tool: user-wide, no floor, observation-first."""
        hits = self._recall_hits(query, self._scope(session), None, timeout, "tool")
        return "\n".join(f"- {hit['text']}" for hit in hits[: self.top_k])

    def _fetch_model(self, model_id: str, timeout: float) -> str:
        key = (self.bank, model_id)
        data = _request(
            "GET", self.bank, f"mental-models/{quote(model_id, safe='')}", None, timeout
        )
        content = (data.get("content") or "").strip()
        name = data.get("name") or model_id
        text = f"### {name}\n{content}" if content else ""
        with _models_lock:
            _models_cache[key] = (time.monotonic(), text)
        return text

    def _models_text(self, deadline: float) -> tuple[str, list[str]]:
        """Cached mental model contents. A cold fetch never waits past the deadline."""
        now = time.monotonic()
        texts, missing, futures = [], [], []
        for model_id in self.mental_models:
            with _models_lock:
                cached = _models_cache.get((self.bank, model_id))
            if cached and now - cached[0] < MODELS_TTL:
                texts.append(cached[1])
            else:
                missing.append(model_id)
                futures.append(
                    _recall_pool.submit(self._fetch_model, model_id, self.timeout)
                )
        if futures:
            done, _ = wait(futures, timeout=max(0.0, deadline - time.monotonic()))
            for model_id, future in zip(missing, futures, strict=True):
                if future in done and not future.exception():
                    texts.append(future.result())
                    continue
                with _models_lock:
                    stale = _models_cache.get((self.bank, model_id))
                if stale:
                    texts.append(stale[1])
        return "\n\n".join(t for t in texts if t), list(self.mental_models)

    def _start_passes(self, session, message: str) -> dict:
        """Start the recall requests: bot-scoped and shared, or one user-wide."""
        submit = _recall_pool.submit
        if self.scope == "user":
            return {
                "user": submit(
                    self._recall_hits,
                    message,
                    self._scope(session),
                    self.min_score,
                    self.timeout,
                    "user",
                )
            }
        return {
            "bot": submit(
                self._recall_hits,
                message,
                self._bot_scope(session),
                self.min_score,
                self.timeout,
                "bot",
            ),
            "shared": submit(
                self._recall_hits,
                message,
                self._scope(session),
                self.min_score_shared,
                self.timeout,
                "shared",
            ),
        }

    def _choose(self, got: dict[str, list[dict]], limit: int) -> list[dict]:
        if self.scope == "user":
            return _merge([], got.get("user", []), limit)
        first_n = max(1, limit - 2)
        return _merge(got.get("bot", [])[:first_n], got.get("shared", []), limit)

    def prefetch_block(self, session, message: str, bot: str = "") -> dict:  # noqa: C901, PLR0912
        """{"text", "details"}: models block plus recalled facts, within the timeout."""
        started = time.monotonic()
        deadline = started + self.timeout
        details: dict = {"outcome": "empty", "ids": [], "facts": []}
        try:
            models = None
            if self.mental_models:
                models = _recall_pool.submit(self._models_text, deadline)
            futures = self._start_passes(session, message)
            models_text = ""
            if models is not None:
                done, _ = wait([models], timeout=max(0, deadline - time.monotonic()))
                if models in done and not models.exception():
                    models_text = models.result()[0]
            limit = (
                min(self.top_k, self.top_k_with_models) if models_text else self.top_k
            )
            done, pending = wait(
                futures.values(), timeout=max(0, deadline - time.monotonic())
            )
            got, errors = {}, 0
            for label, future in futures.items():
                if future not in done:
                    future.cancel()
                elif future.exception():
                    errors += 1
                    logger.warning(
                        "Hindsight prefetch failed for bank %s: %s",
                        self.bank,
                        future.exception(),
                    )
                else:
                    got[label] = future.result()
            hits = self._choose(got, limit)
            if not hits and pending:
                outcome = "timeout"
                logger.warning("Hindsight prefetch timed out for bank %s", self.bank)
            elif not hits and errors:
                outcome = "error"
            else:
                outcome = "ok" if hits or models_text else "empty"
            details.update(
                outcome=outcome,
                ids=[h["id"] for h in hits],
                facts=[
                    {"id": h["id"], "scope": h["scope"], "text": h["text"][:500]}
                    for h in hits
                ],
            )
            if models_text:
                details["mental_models"] = self.mental_models
            self._emit(
                "prefetch",
                outcome,
                duration=time.monotonic() - started,
                count=len(hits),
            )
            facts = "\n".join(f"- {h['text']}" for h in hits)
            if models_text and facts:
                facts = f"{models_text}\n\nRecalled facts:\n{facts}"
            return {"text": facts or models_text, "details": details}
        except Exception:  # prefetch is best effort
            logger.exception("Hindsight prefetch failed for bank %s", self.bank)
            details["outcome"] = "error"
            self._emit("prefetch", "error", duration=time.monotonic() - started)
            return {"text": "", "details": details}

    # -- tools -----------------------------------------------------------------

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
            plugin._retain(ctx.session, text, source="explicit")  # noqa: SLF001
            return "Queued for memory."

        return FunctionToolkit([f.__bot_tool__ for f in (recall, reflect, retain)], ctx)

    def toolkits(self, ctx):
        return [self.make_toolkit(ctx)] if self.enabled else []

    def always_context_sources(self, ctx, message):
        if not self.enabled or not message.strip() or self.prefetch == "off":
            return []
        if self.prefetch == "new_session" and ctx.session.messages.exists():
            self._emit("prefetch", "skipped", reason="not_new_session")
            return []
        if reason := self.machine_turn(getattr(ctx, "turn", None)):
            self._emit("prefetch", "skipped", reason=reason)
            return []
        return [MemorySource(self, ctx.session, message, self.bot.name)]

    # -- retain ----------------------------------------------------------------

    @staticmethod
    def _tool_names(session, call) -> list[str]:
        first = getattr(call, "first_sequence", None)
        if first is None:
            return []
        from django_ergo.conversation.models import MessageBlock

        blocks = MessageBlock.objects.filter(
            message__session=session,
            message__sequence__gte=first,
            block_type="tool_use",
        )
        last = getattr(call, "last_sequence", None)
        if last is not None:
            blocks = blocks.filter(message__sequence__lte=last)
        return list(blocks.values_list("tool_name", flat=True))

    def _skip_retain(self, reason: str) -> None:
        self._emit("retain", "skipped", reason=reason)

    async def after_turn(self, session, message, result) -> None:
        if not self.enabled or self.retain != "after_turn":
            return
        if not message.strip() or not result.text.strip():
            self._skip_retain("empty")
            return
        if result.error or result.needs_approval or result.call is None:
            self._skip_retain("failed")
            return
        meta = result.call.metadata or {}
        if reason := self.machine_turn(meta):
            self._skip_retain(reason)
            return
        try:
            tools = await sync_to_async(self._tool_names, thread_sensitive=True)(
                session, result.call
            )
        except Exception:  # noqa: BLE001 - the filter is an optimisation
            logger.debug("Could not read the turn's tool calls", exc_info=True)
            tools = []
        if tools and set(tools) <= CONFIG_READ_TOOLS and not has_decision(message):
            self._skip_retain("config_only")
            return
        reply = clean_assistant_text(result.text)
        content = f"User: {message}"
        if reply:
            content += f"\nAssistant: {reply}"
        self._retain(session, content, document_id=f"ergonaut-turn-{result.call.pk}")
