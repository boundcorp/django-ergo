"""Scheduled bot messages, set per bot in bot.yaml.

    schedules:
      - name: weekly-meal-plan
        cron: "0 17 * * sun"            # minute hour day-of-month month day-of-week
        message: Propose next week's dinners with the meal-planning skill.
      - name: weekly-stats              # or an ordered list of actions:
        cron: "0 8 * * mon"
        actions:
          - run: tools/analytics.py:pull_stats    # a function in a .py file in the bot folder
            args: {days: 7}                       # (it may take ctx first: a ToolContext)
            stop_if_empty: true                   # nothing returned: skip the rest, quietly
          - prompt: "Summarize last week: {result}"   # {result}: the last run step's value
            to: {thread: "Stats {date:%b %d}"}
        to: main                        # main (default), a named chat, or a new thread:
        # to: {thread: "Reports {n}", in: main}   # {n} run number, {date:%b %d}, strftime codes
        users: [lee]                    # default: permissions.users, else everyone with a chat
                                        # (code-only schedules: once, as the first admin)
        enabled: true

The cron fields are read in each person's timezone (their ``timezone``, else
the bot's ``timezone``, else Django's ``TIME_ZONE``). Fields take ``*``,
numbers, ``a-b`` ranges, ``*/n`` and ``a-b/n`` steps, comma lists, and day and
month names (``mon``, ``jan``); day-of-week 0 and 7 are Sunday. When both day
fields are restricted, either may match (as in cron).

``run_due(bots)`` (Ergonaut's Celery beat calls it every minute) claims each
due run with a ``ScheduleRun`` row per bot, schedule, person and minute (so
nothing runs twice) and runs its actions in order. A prompt goes to its chat
as a thread message with no sender, so it waits for a busy chat like any
other, and Telegram passes the reply on for a main chat. A run step calls a
function from the bot's Python files and is recorded as a ``BotJob``; if it
fails, the steps after it don't run. With ``stop_if_empty``, a run step that
returns nothing (``None``, an empty string, list or dict) ends the run there
too, as a success: an alert check posts only when it found something.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Any
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError

from django_ergo.bots.schedule_workflow import WorkflowError
from django_ergo.bots.schedule_workflow import condition
from django_ergo.bots.schedule_workflow import condition_tree
from django_ergo.bots.schedule_workflow import safe_error
from django_ergo.bots.schedule_workflow import safe_json

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django_ergo.bots.runtime import Bot

logger = logging.getLogger(__name__)

MAX_WORKFLOW_STEPS = 100

NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}  # fmt: skip
FIELDS = [
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day", 1, 31),
    ("month", 1, 12),
    ("weekday", 0, 7),
]


class ScheduleError(ValueError):
    pass


def _value(text: str, low: int, high: int) -> int:
    number = NAMES.get(text.lower()) if not text.isdigit() else int(text)
    if number is None or not low <= number <= high:
        msg = f"{text!r} is not between {low} and {high}"
        raise ScheduleError(msg)
    return number


def _field(text: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        body, _, step_text = part.partition("/")
        step = int(step_text) if step_text else 1
        if step < 1:
            msg = f"bad step in {part!r}"
            raise ScheduleError(msg)
        if body == "*":
            start, end = low, high
        elif "-" in body:
            first, last = body.split("-", 1)
            start, end = _value(first, low, high), _value(last, low, high)
        else:
            start = _value(body, low, high)
            end = high if step_text else start
        if start > end:
            msg = f"{part!r} runs backwards"
            raise ScheduleError(msg)
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class Cron:
    expression: str
    minute: frozenset[int]
    hour: frozenset[int]
    day: frozenset[int]
    month: frozenset[int]
    weekday: frozenset[int]
    day_restricted: bool
    weekday_restricted: bool

    @classmethod
    def parse(cls, expression: str) -> Cron:
        parts = expression.split()
        if len(parts) != len(FIELDS):
            msg = f"cron needs 5 fields (minute hour day month weekday), got {expression!r}"
            raise ScheduleError(msg)
        values = {
            name: _field(text, low, high)
            for text, (name, low, high) in zip(parts, FIELDS, strict=True)
        }
        weekday = frozenset(0 if d == 7 else d for d in values.pop("weekday"))  # noqa: PLR2004
        return cls(
            expression=expression,
            weekday=weekday,
            day_restricted=parts[2] != "*",
            weekday_restricted=parts[4] != "*",
            **values,
        )

    def matches(self, moment: datetime) -> bool:
        return (
            moment.minute in self.minute
            and moment.hour in self.hour
            and moment.month in self.month
            and self.day_matches(moment)
        )

    def day_matches(self, moment: datetime) -> bool:
        day_ok = moment.day in self.day
        weekday_ok = (moment.isoweekday() % 7) in self.weekday
        if self.day_restricted and self.weekday_restricted:
            return day_ok or weekday_ok
        return day_ok and weekday_ok

    def next_after(self, moment: datetime, limit_days: int = 400) -> datetime | None:
        """The next matching minute after ``moment`` (in its timezone), within ``limit_days``."""
        current = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
        end = current + timedelta(days=limit_days)
        while current < end:
            if current.month not in self.month:
                first = current.replace(day=1, hour=0, minute=0)
                current = (first + timedelta(days=32)).replace(day=1)
            elif not self.day_matches(current):
                current = current.replace(hour=0, minute=0) + timedelta(days=1)
            elif current.hour not in self.hour:
                current = current.replace(minute=0) + timedelta(hours=1)
            elif current.minute not in self.minute:
                current += timedelta(minutes=1)
            else:
                return current
        return None


@dataclass(frozen=True)
class Action:
    """One step of a schedule: send a prompt to a chat, or run a function."""

    kind: str  # "prompt" or "run"
    message: str = ""  # prompt: may use {result}, the previous step's result
    to: str = "main"  # prompt: main, a named chat, or "thread"
    thread_title: str = ""  # prompt to a new thread: its title template
    thread_in: str = "main"  # ...and the chat it hangs under
    path: str = ""  # run: a .py file in the bot folder
    function: str = ""  # run: the function in it
    args: dict = field(default_factory=dict)  # run: keyword arguments
    stop_if_empty: bool = False  # run: an empty result ends the run, quietly
    save_as: str = ""
    expression: str = ""
    llm: bool = True

    def title_for(self, moment: datetime, run_number: int) -> str:
        """A new thread's title: ``{n}`` is the run number, ``{date:...}`` and
        strftime codes format the run's local time."""
        try:
            title = self.thread_title.format(n=run_number, date=moment)
            return moment.strftime(title)
        except (KeyError, IndexError, ValueError):
            return f"{self.thread_title} {run_number}"

    @classmethod
    def prompt(cls, name: str, message: str, target, *, llm: bool = True) -> Action:
        target = target or "main"
        thread_title, thread_in = "", "main"
        if isinstance(target, dict):
            thread_title = str(target.get("thread") or "").strip()
            thread_in = str(target.get("in") or "main")
            if not thread_title:
                msg = f"schedule {name}: to: {{thread: ...}} needs a title template"
                raise ScheduleError(msg)
            to = "thread"
        elif str(target) == "new":  # the older spelling: a dated thread per run
            to, thread_title = "thread", f"{name} %b %d"
        else:
            to = "main" if str(target) == "root" else str(target)
        return cls(
            "prompt",
            message=message,
            to=to,
            thread_title=thread_title,
            thread_in="main" if thread_in == "root" else thread_in,
            llm=llm,
        )

    @classmethod
    def run(cls, name: str, spec: str, args, *, stop_if_empty=False) -> Action:
        path, _, function = str(spec).partition(":")
        if (
            not path.endswith(".py")
            or not function
            or path.startswith(("/", ".."))
            or "/../" in path
        ):
            msg = f"schedule {name}: run must be <file.py in the bot folder>:<function>, got {spec!r}"
            raise ScheduleError(msg)
        if args is not None and not isinstance(args, dict):
            msg = f"schedule {name}: args must be a mapping"
            raise ScheduleError(msg)
        return cls(
            "run",
            path=path,
            function=function,
            args=dict(args or {}),
            stop_if_empty=bool(stop_if_empty),
        )

    @property
    def chats(self) -> set[str]:
        """The chats this step needs to exist."""
        if self.kind != "prompt":
            return set()
        return {self.thread_in} if self.to == "thread" else {self.to}


@dataclass(frozen=True)
class Schedule:
    name: str
    cron: Cron
    actions: tuple[Action, ...]
    users: tuple[str, ...] = field(default_factory=tuple)
    enabled: bool = True
    timezone: str = ""

    @classmethod
    def from_config(cls, data: dict) -> Schedule:  # noqa: C901 — mutually exclusive schedule formats
        if not isinstance(data, dict):
            msg = "each schedule needs name, cron and actions (or a message)"
            raise ScheduleError(msg)
        name = str(data.get("name") or "").strip()
        if not name or not data.get("cron"):
            msg = f"schedule {name or '?'} needs name, cron and message"
            raise ScheduleError(msg)
        workflow = data.get("workflow") is not None
        if workflow and ("actions" in data or "message" in data):
            msg = "workflow cannot be combined with actions or message"
            raise ScheduleError(msg)
        if workflow:
            items = data["workflow"]
            if (
                not isinstance(items, list)
                or not items
                or len(items) > MAX_WORKFLOW_STEPS
            ):
                msg = "workflow must be a non-empty list of at most 100 steps"
                raise ScheduleError(msg)
            actions = [cls._workflow_action(name, item) for item in items]
            names = [action.save_as for action in actions if action.save_as]
            if len(names) != len(set(names)):
                msg = "workflow save_as names must be unique"
                raise ScheduleError(msg)
        elif data.get("actions") is not None:
            items = data["actions"]
            if not isinstance(items, list) or not items:
                msg = f"schedule {name}: actions must be a non-empty list"
                raise ScheduleError(msg)
            actions = [cls._action(name, item) for item in items]
        else:
            message = str(data.get("message") or "").strip()
            if not message:
                msg = f"schedule {name} needs name, cron and message"
                raise ScheduleError(msg)
            actions = [Action.prompt(name, message, data.get("to"))]
        tz = str(data.get("timezone") or "")
        if tz:
            try:
                ZoneInfo(tz)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                msg = "schedule timezone must be a valid IANA timezone"
                raise ScheduleError(msg) from exc
        return cls(
            name=name,
            cron=Cron.parse(str(data["cron"])),
            actions=tuple(actions),
            users=tuple(str(u) for u in data.get("users") or []),
            enabled=bool(data.get("enabled", True)),
            timezone=tz,
        )

    @staticmethod
    def _action(name: str, item) -> Action:
        if not isinstance(item, dict):
            msg = f"schedule {name}: each action is {{prompt: ...}} or {{run: ...}}"
            raise ScheduleError(msg)
        if "prompt" in item:
            message = str(item.get("prompt") or "").strip()
            if not message:
                msg = f"schedule {name}: a prompt action needs text"
                raise ScheduleError(msg)
            return Action.prompt(name, message, item.get("to"))
        if "run" in item:
            return Action.run(
                name,
                item["run"],
                item.get("args"),
                stop_if_empty=item.get("stop_if_empty", False),
            )
        msg = f"schedule {name}: each action is {{prompt: ...}} or {{run: ...}}"
        raise ScheduleError(msg)

    @staticmethod
    def _workflow_action(name: str, item) -> Action:
        if (
            not isinstance(item, dict)
            or sum(key in item for key in ("python", "if", "prompt")) != 1
        ):
            msg = "each workflow step needs exactly one of python, if, prompt"
            raise ScheduleError(msg)
        if "if" in item:
            expression = item["if"]
            try:
                condition_tree(expression)
            except WorkflowError as exc:
                raise ScheduleError(str(exc)) from exc
            return Action("if", expression=expression)
        if "prompt" in item:
            if not isinstance(item.get("llm", True), bool):
                msg = "workflow prompt llm must be true or false"
                raise ScheduleError(msg)
            message = str(item["prompt"] or "").strip()
            if not message:
                msg = "workflow prompt needs text"
                raise ScheduleError(msg)
            return Action.prompt(
                name, message, item.get("to"), llm=item.get("llm", True)
            )
        spec = str(item["python"])
        save_as = str(item.get("save_as") or "")
        if save_as and (
            not save_as.isascii()
            or not save_as.isidentifier()
            or save_as.startswith("_")
            or save_as in {"bot", "user", "run", "result", "date", "n"}
        ):
            msg = "save_as needs a public, non-reserved identifier"
            raise ScheduleError(msg)
        args = item.get("args", {})
        if not isinstance(args, dict):
            msg = "workflow python args must be a mapping"
            raise ScheduleError(msg)
        if spec == "ergo.models.check_available":
            return Action("python", function=spec, save_as=save_as, args=dict(args))
        parsed = Action.run(name, spec, args)
        return Action(
            "python",
            path=parsed.path,
            function=parsed.function,
            save_as=save_as,
            args=parsed.args,
        )


def people_for(bot: Bot, schedule: Schedule) -> list:
    """Who a schedule runs for: its users, else the bot's allowed users, else
    everyone with a chat with the bot. A schedule of only ``run`` steps (code,
    no chat) with no users runs once, as the first admin."""
    from django.contrib.auth import get_user_model

    users = get_user_model().objects.filter(is_active=True)
    names = list(schedule.users) or list(bot.definition.allowed_users)
    if names:
        return list(users.filter(username__in=names))
    if all(action.kind != "prompt" for action in schedule.actions):
        return list(users.filter(is_superuser=True).order_by("pk")[:1])
    return list(users.filter(conversation_sessions__bot_name=bot.name).distinct())


def local_now(
    bot: Bot, user, now: datetime, schedule: Schedule | None = None
) -> datetime:
    from django_ergo.bots.tools import ToolContext

    if schedule and schedule.timezone:
        return now.astimezone(ZoneInfo(schedule.timezone))
    return now.astimezone(ToolContext(bot=bot, user=user).timezone)


def run_due(bots: Iterable[Bot], now: datetime | None = None) -> list[str]:
    """Start every schedule due this minute. Returns "bot/schedule/user" for each run.

    Each run is claimed with a ``ScheduleRun`` row, then handed to
    ``DJANGO_ERGO["SCHEDULE_RUNNER"]`` (Ergonaut queues a Celery task), or run
    here and now by default.
    """
    from django.db import IntegrityError
    from django.utils import timezone

    from django_ergo.conversation.models import ScheduleRun
    from django_ergo.settings import api_settings

    now = (now or timezone.now()).replace(second=0, microsecond=0)
    runner = api_settings.SCHEDULE_RUNNER or run_actions
    ran = []
    for bot in bots:
        for schedule in bot.definition.schedules:
            if not schedule.enabled:
                continue
            for user in people_for(bot, schedule):
                moment = local_now(bot, user, now, schedule)
                if not schedule.cron.matches(moment):
                    continue
                try:
                    run = ScheduleRun.objects.create(
                        bot_name=bot.name, schedule=schedule.name, user=user, minute=now
                    )
                except IntegrityError:
                    continue  # already ran this minute (another beat, a retry)
                try:
                    runner(run.pk)
                except Exception as exc:  # noqa: BLE001 — audit failures from configured runners
                    _fail_run(run, bot, safe_error(exc))
                    continue
                ran.append(f"{bot.name}/{schedule.name}/{user.get_username()}")
    return ran


# Keep claiming, ordered execution and audit transitions in one lifecycle.
def run_actions(run_id: int, registry=None) -> None:  # noqa: C901, PLR0912, PLR0915
    """Claim and audit a run once, including deliveries and quiet stops."""
    from django.utils import timezone

    from django_ergo.bots.background import find_bot
    from django_ergo.conversation.models import ScheduleRun

    if not ScheduleRun.objects.filter(pk=run_id, status="pending").update(
        status="in_progress", started_at=timezone.now()
    ):
        return
    run = ScheduleRun.objects.select_related("user").get(pk=run_id)
    bot = None
    try:
        bot = find_bot(run.bot_name, registry)
        schedule = next(
            (s for s in bot.definition.schedules if s.name == run.schedule), None
        )
        if schedule is None:
            msg = "schedule no longer exists"
            raise ScheduleError(msg)  # noqa: TRY301 — missing schedules must fail the claimed run
        moment = local_now(bot, run.user, run.minute, schedule)
        result: Any = None
        values = {}
        for step, action in enumerate(schedule.actions, start=1):
            audit = {
                "step": step,
                "kind": action.kind,
                "status": "in_progress",
                "started_at": timezone.now().isoformat(),
            }
            if action.kind in {"run", "python"}:
                audit["target"] = (
                    f"{action.path}:{action.function}"
                    if action.path
                    else action.function
                )
                if action.save_as:
                    audit["save_as"] = action.save_as
            if action.kind == "if":
                audit["expression"] = action.expression
            if action.kind == "prompt":
                audit.update(to=action.to, llm=action.llm)
            run.steps.append(audit)
            run.save(update_fields=["steps"])
            stop = False
            try:
                if action.kind == "run":
                    job = run_code(
                        bot,
                        action,
                        run.user,
                        name=f"schedule {schedule.name}, step {step}",
                    )
                    audit["job"] = str(job.pk)
                    if job.status != "completed":
                        msg = "legacy job failed"
                        raise WorkflowError(msg)  # noqa: TRY301 — record the failed step before failing the run
                    result = job.result
                    stop = action.stop_if_empty and result in (None, "", [], {})
                    audit["result"] = result
                elif action.kind == "python":
                    context = {
                        **deepcopy(values),
                        "result": deepcopy(result),
                        "bot": bot,
                        "user": run.user,
                        "run": run,
                    }
                    result = _run_python(bot, action, context)
                    audit["result"] = result
                    if action.save_as:
                        values[action.save_as] = result
                elif action.kind == "if":
                    audit["result"] = condition(action.expression, values)
                    stop = not audit["result"]
                else:
                    audit["result"] = _send_prompt(
                        bot, action, run, moment, result, values
                    )
                audit["status"] = "completed"
            except Exception as exc:
                audit.update(status="failed", error=safe_error(exc))
                raise
            finally:
                audit["completed_at"] = timezone.now().isoformat()
                run.save(update_fields=["steps"])
            if stop:
                run.status = "noop"
                break
        else:
            run.status = "completed"
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "completed_at"])
    except Exception as exc:  # noqa: BLE001 — audit failures from arbitrary workflow steps
        _fail_run(run, bot, safe_error(exc))


def _run_python(bot, action, context):
    if action.function == "ergo.models.check_available" and not action.path:
        from django_ergo.bots.model_catalog import check_available

        function = check_available
    else:
        function = getattr(bot.code(action.path), action.function, None)
        if not callable(function):
            msg = "python step function does not exist"
            raise WorkflowError(msg)
    return safe_json(_await_value(function(context, **action.args)))


def _await_value(value):
    import inspect

    if inspect.isawaitable(value):
        from asgiref.sync import async_to_sync

        async def wait():
            return await value

        return async_to_sync(wait)()
    return value


def _fail_run(run, bot, error):
    from django.utils import timezone

    run.status, run.error, run.completed_at = "failed", error, timezone.now()
    run.save(update_fields=["status", "error", "completed_at"])
    if bot is None:
        return
    try:
        _send_prompt(
            bot,
            Action.prompt(
                run.schedule,
                f"Schedule {run.schedule} failed. {error}",
                "main",
                llm=False,
            ),
            run,
            run.minute,
            None,
        )
    except Exception:  # noqa: BLE001 — failure reporting must not replace the audited error
        # Do not leak a second exception while reporting the first.
        logger.warning(
            "Could not deliver failure notification for schedule run %s", run.pk
        )


def run_code(bot: Bot, action: Action, user, *, name: str):
    """Run a function from the bot's Python files, recorded as a BotJob."""
    import inspect

    from django.utils import timezone

    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.models import BotJob

    job = BotJob.objects.create(
        bot_name=bot.name,
        name=name,
        target=f"{action.path}:{action.function}",
        args=safe_json(action.args),
        user=user,
        status="in_progress",
        started_at=timezone.now(),
    )
    try:
        function = getattr(bot.code(action.path), action.function, None)
        if not callable(function):
            msg = f"{action.path} has no function {action.function!r}"
            raise LookupError(msg)  # noqa: TRY301, TRY004
        params = list(inspect.signature(function).parameters)
        args = dict(action.args)
        if params and params[0] == "ctx":
            value = function(ToolContext(bot=bot, user=user), **args)
        else:
            value = function(**args)
        value = _await_value(value)
        from django_ergo.bots.workers import jsonable

        job.status, job.result = "completed", safe_json(jsonable(value))
    except Exception as exc:  # noqa: BLE001 — recorded without exception values
        job.status, job.error = "failed", safe_error(exc)
        job.traceback = job.error
    job.completed_at = timezone.now()
    job.save()
    return job


def _send_prompt(  # noqa: PLR0913
    bot: Bot, action: Action, run, moment, result, values=None
) -> dict:
    import json

    from asgiref.sync import async_to_sync

    from django_ergo.bots import messaging
    from django_ergo.conversation.models import ScheduleRun

    user = run.user
    if action.to == "thread":
        parent = async_to_sync(bot.chat_session)(user, action.thread_in)
        runs = ScheduleRun.objects.filter(
            bot_name=bot.name, schedule=run.schedule, user=user
        ).count()
        session = async_to_sync(bot.create_session)(
            user,
            parent=parent,
            title=action.title_for(moment, runs),
            metadata={"schedule": run.schedule},
        )
    else:
        session = async_to_sync(bot.chat_session)(user, action.to)
    # Literal replacements do not evaluate attribute/index/format expressions,
    # and do not reinterpret braces in JSON results or user-authored text.
    replacements = {
        "{result}": result if isinstance(result, str) else json.dumps(result, indent=2),
        **{
            f"{{{name}}}": json.dumps(value, indent=2)
            for name, value in (values or {}).items()
        },
    }
    import re

    text = re.sub(
        r"\{([A-Za-z][A-Za-z0-9_]*)\}",
        lambda match: replacements.get(match.group(0), match.group(0)),
        action.message,
    )
    if not action.llm:
        from django_ergo.conversation.identity import system_identity
        from django_ergo.conversation.messages import add_message

        message = async_to_sync(add_message)(
            session,
            "assistant",
            [{"block_type": "text", "text": text}],
            author=system_identity(
                f"schedule:{run.schedule}", f"Schedule: {run.schedule}"
            ),
            provenance={"kind": "event"},
        )
    else:
        message = messaging.send_event(
            session,
            text,
            source=f"schedule:{run.schedule}",
            source_label=f"Schedule: {run.schedule}",
            registry=bot.registry,
            metadata={"schedule": run.schedule},
        )
    return {"session": str(session.pk), "message": str(message.pk)}
