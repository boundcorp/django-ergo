"""Background tasks for bot tools: ``@bot_task`` and ``ctx.tasks``.

Mark a function in a bot's tool file with ``@bot_task``; a tool starts it
with ``ctx.tasks`` and waits for it, or awaits it::

    from django_ergo.bots import bot_task, bot_tool

    @bot_task
    def import_receipts(month: str) -> dict:
        ...                                   # slow work, off the chat turn

    @bot_tool(takes_context=True)
    def receipts(ctx, month: str) -> dict:
        job = ctx.tasks.start(import_receipts, month)
        return job.wait(timeout=300)          # or: await job, in async code

    # or both at once: ctx.tasks.run(import_receipts, month, timeout=300)

Arguments and results must be JSON-serializable, since a worker in another
process may run the task. ``DJANGO_ERGO["BOT_TASK_RUNNER"]`` decides where
tasks run (Ergonaut: Celery workers); by default they run in a thread pool
in this process. Workers find the function by bot and task name, so a task
is only ever one the bot's own tool files declared.
"""

from __future__ import annotations

import asyncio
import inspect
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING
from typing import Any

from asgiref.sync import async_to_sync

from django_ergo.bots.tools import bot_task  # noqa: F401 — re-exported
from django_ergo.settings import api_settings

if TYPE_CHECKING:
    from collections.abc import Callable

    from django_ergo.bots.registry import BotRegistry
    from django_ergo.bots.runtime import Bot

_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ergo-bot-task")


class TaskHandle:
    """A started task: ``wait()`` for its result, or ``await`` it."""

    id: str = ""

    def done(self) -> bool:
        raise NotImplementedError

    def wait(self, timeout: float | None = None) -> Any:
        raise NotImplementedError

    def __await__(self):
        return asyncio.to_thread(self.wait).__await__()


class FutureHandle(TaskHandle):
    def __init__(self, future: Future, task_id: str = ""):
        self.future = future
        self.id = task_id or hex(id(future))

    def done(self) -> bool:
        return self.future.done()

    def wait(self, timeout: float | None = None) -> Any:
        return self.future.result(timeout=timeout)


def find_bot(bot_name: str, registry: BotRegistry | None = None) -> Bot:
    from django_ergo.bots import messaging
    from django_ergo.bots import webhooks

    registry = registry or webhooks.get_registry()
    if registry is not None and bot_name in registry:
        return registry.get(bot_name)
    bot = messaging.KNOWN_BOTS.get(bot_name)
    if bot is None:
        msg = f"Bot {bot_name!r} is not loaded"
        raise LookupError(msg)
    return bot


def execute(
    bot_name: str, task_name: str, args: list, kwargs: dict, registry=None
) -> Any:
    """Run a bot's task here and now (what a worker calls)."""
    bot = find_bot(bot_name, registry)
    function = bot.tasks.get(task_name)
    if function is None:
        known = ", ".join(sorted(bot.tasks)) or "none"
        msg = f"{bot_name} has no task {task_name!r} (tasks: {known})"
        raise LookupError(msg)
    if inspect.iscoroutinefunction(function):
        return async_to_sync(function)(*args, **kwargs)
    return function(*args, **kwargs)


def thread_runner(bot: Bot, task_name: str, args: list, kwargs: dict) -> TaskHandle:
    future = _pool.submit(execute, bot.name, task_name, args, kwargs, bot.registry)
    return FutureHandle(future)


class BotTasks:
    """``ctx.tasks``: start a bot's ``@bot_task`` functions in the background."""

    def __init__(self, bot: Bot):
        self.bot = bot

    def _name(self, task: Callable | str) -> str:
        name = task if isinstance(task, str) else getattr(task, "__bot_task__", None)
        if not name or name not in self.bot.tasks:
            msg = f"{task!r} is not a @bot_task of {self.bot.name}"
            raise ValueError(msg)
        return name

    def start(self, task: Callable | str, *args, **kwargs) -> TaskHandle:
        name = self._name(task)
        runner = api_settings.BOT_TASK_RUNNER
        if runner is None:
            return thread_runner(self.bot, name, list(args), kwargs)
        return runner(self.bot.name, name, list(args), kwargs)

    def run(
        self, task: Callable | str, *args, timeout: float | None = None, **kwargs
    ) -> Any:
        return self.start(task, *args, **kwargs).wait(timeout=timeout)
