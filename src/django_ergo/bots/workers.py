"""Workers: long-running work a chat or thread starts and gets an answer from later.

A tool starts a worker and returns at once; the chat shows as busy while it
runs, and when it finishes its result arrives in the chat as a message, so the
bot replies with a follow-up::

    @bot_task
    def build_report(ctx, month: str):        # ctx is a WorkerContext
        ctx.progress("pulling data")
        ...
        return {"rows": 120}

    @bot_tool(takes_context=True)
    def report(ctx, month: str) -> str:
        worker = ctx.workers.start("build_report", title=f"Report {month}", month=month)
        return f"Started {worker.title}; I'll report back when it's done."

A worker function runs once, or **polls**: return ``ctx.again(seconds)`` and it
runs again later, as a fresh task, with ``ctx.state`` (a dict kept on the
Worker) carrying what it learned. Polling suits watching something slow
elsewhere, such as an Orca coding agent: nothing waits in a process, so a
restart loses nothing. ``ctx.stopping`` is true once someone cancelled it.

Functions come from the bot's ``@bot_task`` functions (``task:<name>``) and
from plugins (``<plugin>:<name>``, see ``BotPlugin.worker_functions``).
``DJANGO_ERGO["WORKER_RUNNER"]`` runs them (Ergonaut: Celery); by default a
thread in this process.
"""

from __future__ import annotations

import inspect
import json
import logging
import threading
import traceback
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from django_ergo.settings import api_settings

if TYPE_CHECKING:
    from django_ergo.bots.runtime import Bot
    from django_ergo.conversation.models import ConversationSession
    from django_ergo.conversation.models import Worker

logger = logging.getLogger(__name__)

MAX_POLLS = 10_000
MAX_RESULT_CHARS = 6000


@dataclass
class Again:
    """Run the worker function again after ``seconds``."""

    seconds: float = 30.0
    progress: str = ""


class WorkerContext:
    """What a worker function gets as ``ctx``."""

    def __init__(self, bot: Bot, worker: Worker):
        from django_ergo.bots.tools import ToolContext

        self.bot = bot
        self.worker = worker
        self.session = worker.session
        self.user = worker.session.user
        self.state: dict = dict(worker.state or {})
        self._tools = ToolContext(bot=bot, session=worker.session, user=self.user)

    def progress(self, text: str) -> None:
        """Show what it's doing now (the chat shows the latest line)."""
        from django_ergo.conversation.models import Worker

        self.worker.progress = str(text)[:2000]
        Worker.objects.filter(pk=self.worker.pk).update(progress=self.worker.progress)
        _notify(self.worker)

    def tell(self, text: str) -> None:
        """Send the chat a message now (the bot answers it in a turn), e.g. a question
        the work is waiting on. The worker keeps running."""
        from django_ergo.bots import messaging

        messaging.send(
            None,
            self.session,
            text,
            registry=self.bot.registry,
            metadata={
                "worker": str(self.worker.pk),
                "worker_title": self.worker.title,
                "update": True,
            },
        )

    def again(self, seconds: float = 30.0, progress: str = "") -> Again:
        return Again(seconds=seconds, progress=progress)

    @property
    def stopping(self) -> bool:
        from django_ergo.conversation.models import Worker

        return (
            Worker.objects.filter(pk=self.worker.pk)
            .values_list("status", flat=True)
            .first()
            == "cancelled"
        )

    def secret(self, name: str, default: str | None = None) -> str | None:
        return self._tools.secret(name, default)

    def table(self, name: str):
        return self.bot.table(name)


class WorkerStarter:
    """``ctx.workers`` in a tool: start and list this chat's workers."""

    def __init__(self, bot: Bot, session: ConversationSession | None):
        self.bot = bot
        self.session = session

    def start(
        self,
        function: str,
        *,
        title: str = "",
        notify: bool = True,
        state: dict | None = None,
        **kwargs,
    ) -> Worker:
        """Start a worker. ``function`` is a @bot_task name, ``task:<name>`` or
        ``<plugin>:<name>``; keyword arguments go to it (JSON only)."""
        if self.session is None:
            msg = "Workers belong to a chat; there's no chat here"
            raise ValueError(msg)
        return start(
            self.bot,
            self.session,
            function,
            kwargs,
            title=title,
            notify=notify,
            state=state,
        )

    def list(self, *, active_only: bool = False) -> list[Worker]:
        if self.session is None:
            return []
        qs = self.session.workers.all()
        if active_only:
            qs = qs.filter(status__in=["queued", "running"])
        return list(qs[:50])


def resolve(bot: Bot, function: str):
    """The callable behind ``function`` ("task:x", "plugin:x" or a bare task name)."""
    kind, _, name = function.partition(":")
    if not name:
        kind, name = "task", kind
    if kind == "task":
        found = bot.tasks.get(name)
        if found is None:
            known = ", ".join(sorted(bot.tasks)) or "none"
            msg = f"{bot.name} has no task {name!r} (tasks: {known})"
            raise LookupError(msg)
        return found
    plugin = bot.plugin(kind)
    found = plugin.worker_functions().get(name) if plugin is not None else None
    if found is None:
        msg = f"{bot.name} has no worker function {function!r}"
        raise LookupError(msg)
    return found


def start(  # noqa: PLR0913
    bot: Bot,
    session: ConversationSession,
    function: str,
    args: dict,
    *,
    title: str = "",
    notify: bool = True,
    state: dict | None = None,
) -> Worker:
    from django_ergo.conversation.models import Worker

    if ":" not in function:
        function = f"task:{function}"
    resolve(bot, function)  # fail now, in the tool call, rather than in the background
    json.dumps(args)  # arguments must survive a trip to another process
    worker = Worker.objects.create(
        session=session,
        bot_name=bot.name,
        title=(title or function)[:200],
        function=function,
        args=dict(args),
        notify=notify,
        state=dict(state or {}),
    )
    _notify(worker)
    schedule(worker, 0)
    return worker


def schedule(worker: Worker, delay: float) -> None:
    """Have ``run`` called for the worker after ``delay`` seconds."""
    from django.utils import timezone

    from django_ergo.conversation.models import Worker

    when = timezone.now() + timezone.timedelta(seconds=delay)
    Worker.objects.filter(pk=worker.pk).update(next_poll_at=when)
    runner = api_settings.WORKER_RUNNER
    if runner is not None:
        runner(str(worker.pk), delay)
        return
    timer = threading.Timer(delay, _run_in_thread, args=(str(worker.pk),))
    timer.daemon = True
    timer.start()


def _run_in_thread(worker_id: str) -> None:
    from django.db import close_old_connections

    try:
        run(worker_id)
    finally:
        close_old_connections()


def run(worker_id: str, registry=None) -> str:
    """Run one step of a worker (what a runner calls). Returns its status after."""
    from django.db import transaction
    from django.utils import timezone

    from django_ergo.bots.background import find_bot
    from django_ergo.conversation.models import Worker

    with transaction.atomic():
        worker = (
            Worker.objects.select_for_update(skip_locked=True)
            .select_related("session", "session__user")
            .filter(pk=worker_id)
            .first()
        )
        if worker is None or not worker.active:
            return worker.status if worker else "missing"
        worker.status = "running"
        worker.polls += 1
        worker.started_at = worker.started_at or timezone.now()
        worker.save(update_fields=["status", "polls", "started_at", "updated_at"])
    _notify(worker)
    outcome: Any = None
    error = ""
    try:
        bot = find_bot(worker.bot_name, registry)
        function = resolve(bot, worker.function)
        ctx = WorkerContext(bot, worker)
        params = list(inspect.signature(function).parameters)
        args = dict(worker.args or {})
        outcome = (
            function(ctx, **args) if params and params[0] == "ctx" else function(**args)
        )
        if inspect.isawaitable(outcome):
            from asgiref.sync import async_to_sync

            async def wait(awaitable=outcome):
                return await awaitable

            outcome = async_to_sync(wait)()
        worker.state = ctx.state
    except Exception as exc:  # noqa: BLE001 — recorded on the worker
        error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "Worker %s failed: %s",
            worker_id,
            "".join(traceback.format_exception(exc))[-4000:],
        )
    worker.refresh_from_db(fields=["status", "progress"])
    if worker.status == "cancelled":
        _finish(worker, "cancelled", None, "Cancelled")
        return worker.status
    if not error and isinstance(outcome, Again):
        if worker.polls >= MAX_POLLS:
            _finish(worker, "failed", None, f"Gave up after {worker.polls} polls")
            return worker.status
        if outcome.progress:
            worker.progress = outcome.progress[:2000]
        worker.status = "running"
        worker.save(update_fields=["state", "progress", "status", "updated_at"])
        _notify(worker)
        schedule(worker, max(1.0, float(outcome.seconds)))
        return worker.status
    if error:
        _finish(worker, "failed", None, error)
    else:
        _finish(worker, "completed", _jsonable(outcome), "")
    return worker.status


def cancel(worker: Worker) -> str:
    from django_ergo.conversation.models import Worker

    if not worker.active:
        return f"{worker.title} already {worker.status}."
    Worker.objects.filter(pk=worker.pk, status__in=["queued", "running"]).update(
        status="cancelled"
    )
    _notify(worker)
    return f"Cancelling {worker.title}; it stops at its next check."


def _finish(worker: Worker, status: str, result: Any, error: str) -> None:
    from django.utils import timezone

    from django_ergo.bots import messaging

    worker.status = status
    worker.result = result
    worker.error = error[:4000]
    worker.completed_at = timezone.now()
    worker.next_poll_at = None
    worker.save(
        update_fields=[
            "status",
            "result",
            "error",
            "completed_at",
            "next_poll_at",
            "state",
            "updated_at",
        ]
    )
    _notify(worker)
    if not worker.notify or status == "cancelled":
        return
    if status == "completed":
        shown = (
            result
            if isinstance(result, str)
            else json.dumps(result, indent=2, default=str)
        )
        text = (
            f"Worker “{worker.title}” finished.\n\nResult:\n{shown[:MAX_RESULT_CHARS]}"
        )
    else:
        text = f"Worker “{worker.title}” failed: {error[:MAX_RESULT_CHARS]}"
    from django_ergo.bots.background import find_bot

    try:
        registry = find_bot(worker.bot_name).registry
    except LookupError:
        registry = None
    messaging.send(
        None,
        worker.session,
        text,
        registry=registry,
        metadata={"worker": str(worker.pk), "worker_title": worker.title},
    )


def _notify(worker: Worker) -> None:
    """Tell live views the session changed (Ergonaut wires SESSION_NOTIFIER)."""
    notifier = api_settings.SESSION_NOTIFIER
    if notifier is None:
        return
    try:
        notifier(str(worker.session_id))
    except Exception:  # noqa: BLE001 — live updates are best-effort
        logger.debug("Could not notify about worker %s", worker.pk, exc_info=True)


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    return value


def describe(worker: Worker) -> dict:
    return {
        "id": str(worker.pk),
        "title": worker.title,
        "function": worker.function,
        "status": worker.status,
        "progress": worker.progress,
        "result": worker.result,
        "error": worker.error,
        "created_at": worker.created_at.isoformat(timespec="seconds"),
        "completed_at": worker.completed_at.isoformat(timespec="seconds")
        if worker.completed_at
        else None,
    }


def worker_toolkit(bot: Bot, ctx):
    """The ``workers`` skill: list, start (the bot's @bot_task functions) and cancel."""
    from django_ergo.bots.tools import FunctionToolkit
    from django_ergo.bots.tools import bot_tool

    starter = WorkerStarter(bot, ctx.session)

    @bot_tool(name="ergo_worker_list")
    def list_workers(active_only: bool = False) -> list[dict]:
        """List the workers this chat started (long-running background work) and their status."""
        return [describe(w) for w in starter.list(active_only=active_only)]

    @bot_tool(name="ergo_worker_cancel")
    def cancel_worker(worker_id: str) -> str:
        """Cancel one of this chat's running workers."""
        found = next((w for w in starter.list() if str(w.pk) == str(worker_id)), None)
        if found is None:
            msg = f"No worker {worker_id} in this chat"
            raise ValueError(msg)
        return cancel(found)

    tools = [list_workers, cancel_worker]
    if bot.tasks:
        names = ", ".join(sorted(bot.tasks))

        @bot_tool(
            name="ergo_worker_start",
            description=(
                f"Start one of this bot's background tasks ({names}) as a worker. It runs "
                "while the chat goes on, and its result comes back to this chat as a message "
                "when it's done, so don't wait for it."
            ),
            parameters={
                "task": {"type": "string", "enum": sorted(bot.tasks)},
                "title": {
                    "type": "string",
                    "description": "What it's doing, for the user",
                },
                "args": {
                    "type": "object",
                    "description": "The task's keyword arguments",
                },
            },
            required=["task", "title"],
        )
        def start_worker(task: str, title: str, args: dict | None = None) -> dict:
            return describe(starter.start(task, title=title, **(args or {})))

        tools.append(start_worker)
    return FunctionToolkit([fn.__bot_tool__ for fn in tools], ctx)
