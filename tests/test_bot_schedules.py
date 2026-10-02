from __future__ import annotations

from datetime import UTC
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model

from django_ergo.bots.definition import BotDefinition
from django_ergo.bots.definition import BotDefinitionError
from django_ergo.bots.schedules import Cron
from django_ergo.bots.schedules import ScheduleError
from tests.test_bots import make_bot

User = get_user_model()
LA = ZoneInfo("America/Los_Angeles")


@pytest.mark.parametrize(
    ("expression", "moment", "expected"),
    [
        ("0 17 * * sun", datetime(2026, 10, 4, 17, 0, tzinfo=LA), True),  # a Sunday
        ("0 17 * * sun", datetime(2026, 10, 5, 17, 0, tzinfo=LA), False),
        ("0 17 * * 7", datetime(2026, 10, 4, 17, 0, tzinfo=LA), True),
        (
            "*/15 9-17 * * mon-fri",
            datetime(2026, 10, 2, 9, 45, tzinfo=LA),
            True,
        ),  # a Friday
        ("*/15 9-17 * * mon-fri", datetime(2026, 10, 2, 9, 50, tzinfo=LA), False),
        ("30 8 1,15 * *", datetime(2026, 10, 15, 8, 30, tzinfo=LA), True),
        ("0 0 1 jan *", datetime(2027, 1, 1, 0, 0, tzinfo=LA), True),
        (
            "0 12 13 * fri",
            datetime(2026, 10, 13, 12, 0, tzinfo=LA),
            True,
        ),  # day OR weekday
        ("0 12 13 * fri", datetime(2026, 10, 2, 12, 0, tzinfo=LA), True),
    ],
)
def test_cron_matches(expression, moment, expected):
    assert Cron.parse(expression).matches(moment) is expected


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "* * * *",
        "61 * * * *",
        "* 24 * * *",
        "*/0 * * * *",
        "5-2 * * * *",
        "0 0 * * xyz",
    ],
)
def test_bad_cron_is_refused(bad):
    with pytest.raises(ScheduleError):
        Cron.parse(bad)


def test_next_run():
    cron = Cron.parse("0 17 * * sun")
    assert cron.next_after(datetime(2026, 10, 2, 9, 0, tzinfo=LA)) == datetime(
        2026, 10, 4, 17, 0, tzinfo=LA
    )
    assert (
        Cron.parse("0 0 29 feb *").next_after(datetime(2026, 10, 2, tzinfo=LA)) is None
    )  # past 400 days
    assert Cron.parse("0 0 29 feb *").next_after(
        datetime(2027, 10, 2, tzinfo=LA)
    ) == datetime(2028, 2, 29, 0, 0, tzinfo=LA)
    assert (
        Cron.parse("*/5 * * * *")
        .next_after(datetime(2026, 10, 2, 9, 3, tzinfo=LA))
        .minute
        == 5
    )


def test_schedules_in_bot_yaml_are_checked():
    good = BotDefinition.from_dict(
        {
            "name": "k",
            "schedules": [
                {"name": "plan", "cron": "0 17 * * sun", "message": "Plan", "to": "new"}
            ],
        }
    )
    assert [(s.name, s.to) for s in good.schedules] == [("plan", "new")]
    for bad, error in [
        ([{"name": "plan", "cron": "0 17 * *", "message": "x"}], "5 fields"),
        ([{"name": "plan", "message": "x"}], "needs name, cron and message"),
        (
            [{"name": "plan", "cron": "* * * * *", "message": "x", "to": "everyone"}],
            "root or new",
        ),
        ([{"name": "p", "cron": "* * * * *", "message": "x"}] * 2, "two named"),
    ]:
        with pytest.raises(BotDefinitionError, match=error):
            BotDefinition.from_dict({"name": "k", "schedules": bad})


SCHEDULED = """
    name: kitchen
    timezone: America/Los_Angeles
    tools: [tools/pantry.py]
    schedules:
      - {name: weekly-plan, cron: "0 17 * * sun", message: "Plan next week's dinners."}
      - {name: shopping, cron: "0 9 * * sat", message: "What do we need?", to: new, users: [lee]}
      - {name: paused, cron: "* * * * *", message: "Never", enabled: false}
"""


@pytest.mark.django_db(transaction=True)
def test_run_due_sends_each_schedule_once(tmp_path, settings):
    from django_ergo.bots import schedules
    from django_ergo.conversation.models import ConversationSession
    from django_ergo.conversation.models import ThreadMessage

    settings.DJANGO_ERGO = {"THREAD_MESSAGE_RUNNER": "tests.test_bots.record_message"}
    bot, _ = make_bot(tmp_path, yaml_text=SCHEDULED)
    lee = User.objects.create(username="lee")
    User.objects.create(username="guest")  # no chat with the bot: not scheduled
    ConversationSession.objects.create(
        user=lee, bot_name="kitchen", metadata={"bot_role": "root"}
    )

    sunday_5pm = datetime(2026, 10, 4, 17, 0, tzinfo=LA).astimezone(UTC)
    assert schedules.run_due([bot], sunday_5pm) == ["kitchen/weekly-plan/lee"]
    assert schedules.run_due([bot], sunday_5pm) == []  # once per minute
    sent = ThreadMessage.objects.get()
    assert sent.sender_session_id is None
    assert sent.metadata == {"schedule": "weekly-plan"}
    assert sent.recipient_session.metadata["bot_role"] == "root"

    saturday_9am = datetime(2026, 10, 3, 9, 0, tzinfo=LA).astimezone(UTC)
    assert schedules.run_due([bot], saturday_9am) == ["kitchen/shopping/lee"]
    thread = ThreadMessage.objects.get(metadata__schedule="shopping").recipient_session
    assert thread.metadata["bot_role"] == "thread"
    assert thread.metadata["title"] == "shopping Oct 03"

    from django_ergo.bots.messaging import turn_text

    assert turn_text(sent).startswith("[Scheduled message: weekly-plan.")
