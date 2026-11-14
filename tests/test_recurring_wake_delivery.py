"""Consumer-visible recurring checks use ordinary turns without notification spam."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from django_ergo.bots import archival
from django_ergo.bots import messaging
from django_ergo.bots import recurring_wakes
from django_ergo.conversation.models import ThreadMessage
from tests.test_bot_wakes import chat
from tests.test_bots import make_bot
from tests.test_bots import say
from tests.test_bots import thread_messages  # noqa: F401
from tests.test_conversation_structured import claude_tool

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("thread_messages"),
]


@pytest.mark.parametrize(
    ("unchanged", "checkins", "kind", "expected"),
    [
        (True, False, "message", ""),
        (False, False, "message", "A new deployment is ready"),
        (True, True, "message", "A new deployment is ready"),
        (True, False, "question", "A new deployment is ready"),
    ],
)
def test_recurring_unchanged_is_quiet_but_changes_and_questions_remain_visible(
    tmp_path, unchanged, checkins, kind, expected
):
    response = claude_tool(
        "send_reply",
        {"type": kind, "text": "A new deployment is ready", "unchanged": unchanged},
    )
    bot, engine = make_bot(tmp_path, response)
    user = get_user_model().objects.create(username="checker")
    session = bot.sessions().create(
        user=user, bot_name=bot.name, metadata={"bot_role": "main"}
    )
    now = timezone.now()
    recurring_wakes.create(
        session,
        "Check deployments",
        interval_minutes=1,
        max_occurrences=1,
        periodic_checkins=checkins,
        now=now - timedelta(minutes=1),
    )
    recurring_wakes.fire_due(now)
    message = ThreadMessage.objects.get(recipient_session=session)
    messaging.deliver(str(message.pk))
    message.refresh_from_db()
    assert message.status == "answered"
    if expected:
        assert expected in message.reply_text
    else:
        assert message.reply_text == ""
    assert bot.plugins[0].events[-1][2] == expected
    output_text = list(
        session.messages.filter(role="assistant").values_list(
            "content_blocks__text", flat=True
        )
    )
    # Quiet results retain structured-call evidence, not a visible all-clear message.
    if not expected:
        assert not any(
            "A new deployment is ready" in str(content) for content in output_text
        )
    assert len(engine._client.calls) == 1
    assert recurring_wakes.fire_due(now + timedelta(minutes=5)) == []


def test_archive_cancels_queued_recurring_checks_and_never_reopens_thread(tmp_path):
    _, engine, session = chat(tmp_path, "archive-check", say("must not run"))
    now = timezone.now()
    recurring_wakes.create(
        session,
        "Check",
        interval_minutes=1,
        max_occurrences=2,
        now=now - timedelta(minutes=1),
    )
    recurring_wakes.fire_due(now)
    message = ThreadMessage.objects.get(recipient_session=session)
    archival.archive(session)
    messaging.deliver(str(message.pk))
    session.refresh_from_db()
    message.refresh_from_db()
    assert session.status == "completed"
    assert message.status == "failed"
    assert engine._client.calls == []
    assert recurring_wakes.list_schedules(session)[0]["status"] == "cancelled"


def test_old_recurring_approval_stays_live_during_approved_tool(tmp_path, monkeypatch):
    from asgiref.sync import async_to_sync

    from django_ergo.bots.tools import FunctionToolkit
    from django_ergo.conversation.models import RecurringWakeOccurrence
    from django_ergo.conversation.models import StructuredCall

    bot, engine, session = chat(
        tmp_path,
        "old-approval",
        claude_tool("add_to_list", {"item": "milk", "qty": 2}, tool_id="approved"),
        say("Added the milk."),
    )
    now = timezone.now()
    schedule = recurring_wakes.create(
        session,
        "Check the shopping list",
        interval_minutes=1,
        max_occurrences=1,
        now=now - timedelta(minutes=1),
    )
    recurring_wakes.fire_due(now)
    occurrence = RecurringWakeOccurrence.objects.get(schedule_id=schedule["id"])
    message = occurrence.thread_message
    messaging.deliver(str(message.pk))
    message.refresh_from_db()
    assert message.status == "waiting"
    call = StructuredCall.objects.get(
        session=session, metadata__thread_message=str(message.pk)
    )
    assert call.status == "awaiting_approval"
    old = timezone.now() - timedelta(seconds=messaging.STALE_TURN_SECONDS + 60)
    StructuredCall.objects.filter(pk=call.pk).update(updated_at=old)
    ThreadMessage.objects.filter(pk=message.pk).update(updated_at=old)
    observed = []
    execute_tool = FunctionToolkit.execute_tool

    def execute_with_sweep(toolkit, tool_name, arguments):
        if tool_name == "add_to_list":
            # Run the real approved action, then sweep before returning its result
            # to the resumed model. An old claim timestamp retires this live turn.
            result = execute_tool(toolkit, tool_name, arguments)
            fired = recurring_wakes.fire_due(timezone.now())
            occurrence.refresh_from_db()
            message.refresh_from_db()
            observed.append(
                (
                    fired,
                    occurrence.status,
                    occurrence.completed_at,
                    message.status,
                    recurring_wakes.list_schedules(session)[0]["status"],
                    len(engine._client.calls),
                )
            )
            return result
        return execute_tool(toolkit, tool_name, arguments)

    monkeypatch.setattr(FunctionToolkit, "execute_tool", execute_with_sweep)
    done = async_to_sync(bot.resume)(session, {"approved": True})
    assert observed == [([], "waiting", None, "waiting", "active", 1)]
    assert done.text == "Added the milk."
    message.refresh_from_db()
    occurrence.refresh_from_db()
    assert message.status == "answered"
    assert message.reply_text == "Added the milk."
    assert occurrence.status == "answered"
    assert recurring_wakes.list_schedules(session)[0]["status"] == "completed"
    assert len(engine._client.calls) == 2
    tool_result = engine._client.calls[-1]["messages"][-1]["content"][0]
    assert tool_result["content"] == "old-approval added 2 milk"


def test_dispatch_failure_blocks_recurring_schedule_without_retry(
    tmp_path, monkeypatch
):
    from django_ergo.settings import api_settings

    _, engine, session = chat(tmp_path, "dispatch-check")
    now = timezone.now()
    recurring_wakes.create(
        session,
        "Check",
        interval_minutes=1,
        max_occurrences=2,
        now=now - timedelta(minutes=1),
    )

    def broken_runner(message_id):
        msg = "broker disconnected"
        raise ConnectionError(msg)

    monkeypatch.setattr(api_settings, "THREAD_MESSAGE_RUNNER", broken_runner)
    recurring_wakes.fire_due(now)
    assert ThreadMessage.objects.get(recipient_session=session).status == "failed"
    assert recurring_wakes.list_schedules(session)[0]["status"] == "blocked"
    assert recurring_wakes.fire_due(now + timedelta(minutes=5)) == []
    assert engine._client.calls == []


def test_chat_tool_creates_user_requested_finite_checks(tmp_path):
    from asgiref.sync import async_to_sync

    _, _, session = chat(tmp_path, "tool-check")
    bot, _ = make_bot(
        tmp_path / "tool",
        claude_tool(
            "ergo_wake_recurring",
            {
                "note": "Check this chat for deployment changes",
                "interval_minutes": 5,
                "duration_minutes": 600,
            },
        ),
        say("I'll check every 5 minutes for 10 hours; unchanged checks stay quiet."),
    )
    async_to_sync(bot.ask)(
        session, "Check this chat every 5 minutes for the next 10 hours"
    )
    schedule = recurring_wakes.list_schedules(session)[0]
    assert schedule["interval_seconds"] == 300
    expiry = timezone.make_naive(schedule["expires_at"])
    assert 35990 <= (expiry - timezone.now()).total_seconds() <= 36000
    assert schedule["status"] == "active"
    assert schedule["periodic_checkins"] is False


def test_lifecycle_correlates_due_queue_claim_and_actual_response(tmp_path, settings):
    from django.conf import settings as django_settings

    from django_ergo.conversation.models import RecurringWakeOccurrence
    from django_ergo.models import LifecycleEvent

    settings.DJANGO_ERGO = {
        **django_settings.DJANGO_ERGO,
        "LIFECYCLE_EVENTS_ENABLED": True,
    }
    bot, engine, session = chat(tmp_path, "lifecycle-check", say("A meaningful change"))
    now = timezone.now()
    schedule = recurring_wakes.create(
        session,
        "Check",
        interval_minutes=1,
        max_occurrences=1,
        now=now - timedelta(minutes=2, seconds=20),
    )
    recurring_wakes.fire_due(now)
    occurrence = RecurringWakeOccurrence.objects.get(schedule_id=schedule["id"])
    message = occurrence.thread_message
    queued = LifecycleEvent.objects.get(key=f"queued:{occurrence.pk}")
    requested = LifecycleEvent.objects.get(key=f"requested:{occurrence.pk}")
    assert requested.occurred_at == queued.occurred_at
    assert occurrence.scheduled_for < requested.occurred_at
    assert queued.occurred_at == occurrence.dispatched_at
    assert queued.message_id == str(message.pk)
    assert not LifecycleEvent.objects.filter(
        message_id=str(message.pk), stage="started"
    ).exists()
    messaging.deliver(str(message.pk))
    stages = set(
        LifecycleEvent.objects.filter(message_id=str(message.pk)).values_list(
            "stage", flat=True
        )
    )
    assert {
        "requested",
        "queued",
        "dispatched",
        "delivered",
        "started",
        "resumed",
        "result_persisted",
    } <= stages
    assert len(engine._client.calls) == 1
    assert bot.plugins[0].events[-1][2] == "A meaningful change"


@pytest.mark.parametrize(
    ("unchanged", "periodic_checkins", "kind", "should_notify"),
    [
        (True, False, "message", False),
        (False, False, "message", True),
        (True, True, "message", True),
        (True, False, "question", True),
    ],
)
def test_telegram_recurring_changes_questions_and_opted_in_checkins_notify(
    tmp_path, unchanged, periodic_checkins, kind, should_notify
):
    from asgiref.sync import async_to_sync

    from tests.test_bot_plugins import telegram_bot

    bot, engine, plugin = telegram_bot(
        tmp_path,
        claude_tool(
            "send_reply",
            {"type": kind, "text": "Deployment update", "unchanged": unchanged},
        ),
    )
    user = get_user_model().objects.create(username="cook")
    session = async_to_sync(bot.root_session)(user)
    now = timezone.now()
    recurring_wakes.create(
        session,
        "Check deployments",
        interval_minutes=1,
        max_occurrences=1,
        periodic_checkins=periodic_checkins,
        now=now - timedelta(minutes=1),
    )
    recurring_wakes.fire_due(now)
    message = ThreadMessage.objects.get(recipient_session=session)
    messaging.deliver(str(message.pk))
    sent = plugin.api.sent()
    if should_notify:
        assert sent[-1]["text"] == "Deployment update"
        assert str(sent[-1]["chat_id"]) == "111"
    else:
        assert sent == []
    assert len(engine._client.calls) == 1


def test_recurring_turn_cannot_renew_itself_via_one_shot_wake(tmp_path):
    from django_ergo.conversation.models import Wake

    bot, engine, session = chat(
        tmp_path,
        "renewal-check",
        claude_tool(
            "ergo_wake_later",
            {"note": "Create another recurring schedule", "delay_minutes": 1},
        ),
        say("I need a new user request to keep checking."),
    )
    now = timezone.now()
    recurring_wakes.create(
        session,
        "Check once",
        interval_minutes=1,
        max_occurrences=1,
        now=now - timedelta(minutes=1),
    )
    recurring_wakes.fire_due(now)
    message = ThreadMessage.objects.get(recipient_session=session)
    messaging.deliver(str(message.pk))
    assert not Wake.objects.filter(session=session).exists()
    assert recurring_wakes.list_schedules(session)[0]["status"] == "completed"
    assert bot.plugins[0].events[-1][2] == "I need a new user request to keep checking."
    assert len(engine._client.calls) == 2


@pytest.mark.parametrize("expiry_mode", ["duration", "explicit_offset"])
def test_recurring_tool_uses_elapsed_utc_on_non_utc_naive_host(
    tmp_path, settings, expiry_mode
):
    from datetime import UTC
    from datetime import datetime

    from django_ergo.bots.tools import ToolContext
    from django_ergo.bots.wakes import wake_toolkit

    settings.TIME_ZONE = "America/New_York"
    settings.USE_TZ = False
    bot, _, session = chat(tmp_path, "timezone-check")
    before = datetime.now(UTC)
    arguments = {"note": "Check", "interval_minutes": 5}
    if expiry_mode == "duration":
        arguments["duration_minutes"] = 600
    else:
        arguments["expires_at"] = (before + timedelta(hours=10)).isoformat()
    toolkit = wake_toolkit(ToolContext(bot=bot, session=session, user=session.user))
    toolkit.execute_tool("ergo_wake_recurring", arguments)
    schedule = recurring_wakes.list_schedules(session)[0]
    assert 300 <= (schedule["next_run_at"] - before).total_seconds() <= 305
    assert 36000 <= (schedule["expires_at"] - before).total_seconds() <= 36005


def test_recurring_ancestry_prevents_delegated_and_reply_renewal_without_quieting_results(
    tmp_path,
):
    from django_ergo.conversation.models import RecurringWake
    from django_ergo.conversation.models import Wake

    renewal = {"note": "Keep checking", "interval_minutes": 1, "max_occurrences": 2}
    bot, engine, session = chat(
        tmp_path,
        "delegated-renewal",
        claude_tool(
            "ergo_thread_send",
            {"thread": "new", "title": "Check once", "message": "Check once"},
        ),
        say("Delegated one check."),
        claude_tool("ergo_wake_recurring", renewal, tool_id="child-renew"),
        claude_tool("send_reply", {"text": "Checked once", "unchanged": True}),
        claude_tool("ergo_wake_recurring", renewal, tool_id="reply-renew"),
        claude_tool("ergo_wake_later", {"note": "Renew later", "delay_minutes": 1}),
        say("Further checks need a new user request."),
    )
    now = timezone.now()
    schedule = recurring_wakes.create(
        session,
        "Delegate one check",
        interval_minutes=1,
        max_occurrences=1,
        now=now - timedelta(minutes=1),
    )
    recurring_wakes.fire_due(now)
    occurrence_message = ThreadMessage.objects.get(recipient_session=session)
    messaging.deliver(str(occurrence_message.pk))
    delegated = ThreadMessage.objects.exclude(recipient_session=session).get()
    assert delegated.metadata["recurring_origin"] == schedule["id"]
    assert "recurring_wake" not in delegated.metadata
    assert "recurring_occurrence" not in delegated.metadata
    messaging.deliver(str(delegated.pk))
    delegated.refresh_from_db()
    assert delegated.reply_text == "Checked once"
    reply = ThreadMessage.objects.get(in_reply_to=delegated)
    assert reply.metadata["recurring_origin"] == schedule["id"]
    messaging.deliver(str(reply.pk))
    assert RecurringWake.objects.count() == 1
    assert not Wake.objects.exists()
    assert len(engine._client.calls) == 7
    assert bot.plugins[0].events[-1][2] == "Further checks need a new user request."
