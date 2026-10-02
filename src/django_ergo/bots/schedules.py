"""Scheduled bot messages, set per bot in bot.yaml.

    schedules:
      - name: weekly-meal-plan
        cron: "0 17 * * sun"            # minute hour day-of-month month day-of-week
        message: Propose next week's dinners with the meal-planning skill.
        to: main                        # main (default), a named chat, or a new thread:
        # to: {thread: "Reports {n}", in: main}   # {n} run number, {date:%b %d}, strftime codes
        users: [lee]                    # default: permissions.users, else everyone with a chat
        enabled: true

The cron fields are read in each person's timezone (their ``timezone``, else
the bot's ``timezone``, else Django's ``TIME_ZONE``). Fields take ``*``,
numbers, ``a-b`` ranges, ``*/n`` and ``a-b/n`` steps, comma lists, and day and
month names (``mon``, ``jan``); day-of-week 0 and 7 are Sunday. When both day
fields are restricted, either may match (as in cron).

``run_due(bots)`` (Ergonaut's Celery beat calls it every minute) sends each
due schedule's message to the chosen session as a thread message with no
sender, so it waits for a busy chat like any other, and Telegram passes the
result on for a root chat. A ``ScheduleRun`` row per bot, schedule, person and
minute keeps a schedule from running twice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django_ergo.bots.runtime import Bot

logger = logging.getLogger(__name__)

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
class Schedule:
    name: str
    cron: Cron
    message: str
    to: str = "main"  # main, a named chat, or "thread"
    users: tuple[str, ...] = field(default_factory=tuple)
    enabled: bool = True
    thread_title: str = ""  # for to: thread, a template for each new thread's title
    thread_in: str = "main"  # the chat new threads hang under

    def title_for(self, moment: datetime, run_number: int) -> str:
        """A new thread's title: ``{n}`` is the run number, ``{date:...}`` and
        strftime codes format the run's local time."""
        try:
            title = self.thread_title.format(n=run_number, date=moment)
            return moment.strftime(title)
        except (KeyError, IndexError, ValueError):
            return f"{self.thread_title} {run_number}"

    @classmethod
    def from_config(cls, data: dict) -> Schedule:
        if not isinstance(data, dict):
            msg = "each schedule needs name, cron and message"
            raise ScheduleError(msg)
        name = str(data.get("name") or "").strip()
        message = str(data.get("message") or "").strip()
        if not name or not message or not data.get("cron"):
            msg = f"schedule {name or '?'} needs name, cron and message"
            raise ScheduleError(msg)
        target = data.get("to") or "main"
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
            name=name,
            cron=Cron.parse(str(data["cron"])),
            message=message,
            to=to,
            users=tuple(str(u) for u in data.get("users") or []),
            enabled=bool(data.get("enabled", True)),
            thread_title=thread_title,
            thread_in="main" if thread_in == "root" else thread_in,
        )


def people_for(bot: Bot, schedule: Schedule) -> list:
    """Who a schedule runs for: its users, else the bot's allowed users, else
    everyone with a chat with the bot."""
    from django.contrib.auth import get_user_model

    users = get_user_model().objects.filter(is_active=True)
    names = list(schedule.users) or list(bot.definition.allowed_users)
    if names:
        return list(users.filter(username__in=names))
    return list(users.filter(conversation_sessions__bot_name=bot.name).distinct())


def local_now(bot: Bot, user, now: datetime) -> datetime:
    from django_ergo.bots.tools import ToolContext

    return now.astimezone(ToolContext(bot=bot, user=user).timezone)


def run_due(bots: Iterable[Bot], now: datetime | None = None) -> list[str]:
    """Send every schedule due this minute. Returns "bot/schedule/user" for each run."""
    from asgiref.sync import async_to_sync
    from django.db import IntegrityError
    from django.utils import timezone

    from django_ergo.bots import messaging
    from django_ergo.conversation.models import ScheduleRun

    now = (now or timezone.now()).replace(second=0, microsecond=0)
    ran = []
    for bot in bots:
        for schedule in bot.definition.schedules:
            if not schedule.enabled:
                continue
            for user in people_for(bot, schedule):
                if not schedule.cron.matches(local_now(bot, user, now)):
                    continue
                try:
                    ScheduleRun.objects.create(
                        bot_name=bot.name, schedule=schedule.name, user=user, minute=now
                    )
                except IntegrityError:
                    continue  # already ran this minute (another beat, a retry)
                try:
                    if schedule.to == "thread":
                        parent = async_to_sync(bot.chat_session)(
                            user, schedule.thread_in
                        )
                        runs = ScheduleRun.objects.filter(
                            bot_name=bot.name, schedule=schedule.name, user=user
                        ).count()
                        session = async_to_sync(bot.create_session)(
                            user,
                            parent=parent,
                            title=schedule.title_for(local_now(bot, user, now), runs),
                            metadata={"schedule": schedule.name},
                        )
                    else:
                        session = async_to_sync(bot.chat_session)(user, schedule.to)
                    messaging.send(
                        None,
                        session,
                        schedule.message,
                        registry=bot.registry,
                        metadata={"schedule": schedule.name},
                    )
                except Exception:
                    logger.exception(
                        "Schedule %s of %s failed", schedule.name, bot.name
                    )
                    continue
                ran.append(f"{bot.name}/{schedule.name}/{user.get_username()}")
    return ran
