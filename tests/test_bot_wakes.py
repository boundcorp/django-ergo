from __future__ import annotations

import json
import threading
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.db import close_old_connections
from django.db import transaction
from django.utils import timezone

from django_ergo.bots import messaging
from django_ergo.bots import wakes
from django_ergo.conversation.models import SessionMessage
from django_ergo.conversation.models import StructuredCall
from django_ergo.conversation.models import ThreadMessage
from django_ergo.conversation.models import Wake
from tests.test_bots import SENT
from tests.test_bots import _texts
from tests.test_bots import make_bot
from tests.test_bots import say
from tests.test_bots import thread_messages  # noqa: F401 — the fixture
from tests.test_conversation_structured import claude_tool

User = get_user_model()

pytestmark = pytest.mark.usefixtures("thread_messages")


def later(minutes: float) -> datetime:
    return timezone.now() + timedelta(minutes=minutes)


def chat(tmp_path, username="wake", *responses):
    bot, engine = make_bot(tmp_path, *responses)
    user = User.objects.create(username=username)
    session = bot.sessions().create(
        user=user, bot_name=bot.name, metadata={"bot_role": "main"}
    )
    return bot, engine, session


def human_says(session, text="Any news?"):
    last = session.messages.order_by("-sequence").first()
    return SessionMessage.objects.create(
        session=session,
        role="user",
        sequence=(last.sequence + 1) if last else 0,
        author={"kind": "django_user", "ref": str(session.user_id)},
    )


def fire_now(minutes_ahead: float = 0):
    return wakes.fire_due(later(minutes_ahead))


@pytest.mark.django_db(transaction=True)
async def test_a_bot_wakes_itself_once_with_its_note(tmp_path):
    user = await User.objects.acreate(username="ads")
    bot, engine = make_bot(
        tmp_path,
        claude_tool(
            "ergo_wake_later",
            {"note": "Meta rate limit: retry the ad set upload", "delay_minutes": 5},
        ),
        say("Rate limited; I'll pick this up in 5 minutes."),
        say("Uploaded."),
    )
    main = await bot.main_session(user)

    await bot.ask(main, "Upload the new ad set")

    assert {"ergo_wake_later", "ergo_wake_cancel"} <= {
        t["name"] for t in engine._client.calls[0]["tools"]
    }
    wake = await Wake.objects.aget(session=main)
    assert (wake.status, wake.chain) == ("pending", 1)
    assert await sync_to_async(wakes.pending_wakes)(main) == [wakes.describe(wake)]
    assert await sync_to_async(fire_now)(4) == []  # not due yet

    fired = await sync_to_async(fire_now)(6)
    assert fired == [str(wake.id)]
    assert await sync_to_async(fire_now)(7) == []  # a second beat tick
    assert await sync_to_async(wakes.fire)(wake.id, now=later(8)) is None
    message = await ThreadMessage.objects.aget(recipient_session=main)
    assert list(SENT) == [str(message.id)]

    await sync_to_async(messaging.deliver)(SENT.pop(0))

    await wake.arefresh_from_db()
    await message.arefresh_from_db()
    assert (wake.status, wake.thread_message_id) == ("fired", message.id)
    assert (message.status, message.reply_text) == ("answered", "Uploaded.")
    heard = next(t for t in _texts(engine._client.calls[-1]) if "Self-wake" in t)
    assert "[Event from Self-wake: automated notification, not a message" in heard
    assert "Nobody has written in this chat since you scheduled it." in heard
    assert heard.endswith("Your note:\nMeta rate limit: retry the ad set upload")
    assert await sync_to_async(wakes.pending_wakes)(main) == []


@pytest.mark.django_db(transaction=True)
def test_the_wake_says_when_the_user_wrote_meanwhile(tmp_path):
    _, _, session = chat(tmp_path)
    wakes.schedule(session, "check the deploy", delay_minutes=10)
    human_says(session)

    wakes.fire_due(later(11))

    text = ThreadMessage.objects.get(recipient_session=session).text
    assert "The user has written in this chat since you scheduled it" in text
    assert text.startswith("[Self-wake: you scheduled this turn")


@pytest.mark.django_db(transaction=True)
def test_concurrent_firers_send_one_message(tmp_path):
    _, _, session = chat(tmp_path)
    wake = wakes.schedule(session, "retry", delay_minutes=1)
    start = threading.Barrier(4)
    results = []

    def fire():
        try:
            start.wait()
            results.append(wakes.fire(wake.id, now=later(2)))
        finally:
            close_old_connections()

    runners = [threading.Thread(target=fire) for _ in range(4)]
    for runner in runners:
        runner.start()
    for runner in runners:
        runner.join()

    assert sum(result is not None for result in results) == 1
    assert ThreadMessage.objects.filter(recipient_session=session).count() == 1
    assert len(SENT) == 1


@pytest.mark.django_db(transaction=True)
def test_wakes_in_a_row_are_capped_until_a_human_or_a_schedule_writes(tmp_path):
    _, _, session = chat(tmp_path)
    other = session.__class__.objects.create(user=session.user, bot_name="kitchen")
    for chain in range(1, wakes.MAX_CHAIN + 1):
        wake = wakes.schedule(session, f"step {chain}", delay_minutes=1)
        assert wake.chain == chain
        assert wakes.fire(wake.id, now=later(2 * chain)) is not None
        # As if it fired just now (the test fires it ahead of the clock).
        Wake.objects.filter(id=wake.id).update(fired_at=timezone.now())
        # Messages the bot can cause itself don't reset the count.
        messaging.send(other, session, "a reply from another thread")

    with pytest.raises(ValueError, match="3 wakes have already run in a row"):
        wakes.schedule(session, "again", delay_minutes=1)
    assert not Wake.objects.filter(status="pending").exists()

    human_says(session)
    assert wakes.schedule(session, "fresh", delay_minutes=1).chain == 1
    wakes.cancel(session)

    # A bot.yaml schedule prompt resets it too: the bot can't send one itself.
    Wake.objects.filter(session=session).update(chain=wakes.MAX_CHAIN)
    Wake.objects.filter(session=session, fired_at__isnull=False).update(
        fired_at=timezone.now()
    )
    with pytest.raises(ValueError, match="in a row"):
        wakes.schedule(session, "again", delay_minutes=1)
    messaging.send_event(
        session,
        "Daily report",
        source="schedule:daily",
        source_label="Schedule: daily",
        metadata={"schedule": "daily"},
    )
    assert wakes.schedule(session, "after the schedule", delay_minutes=1).chain == 1


@pytest.mark.django_db(transaction=True)
def test_threads_that_wake_and_start_threads_stop_at_the_delegation_cap(tmp_path):
    """Waking keeps the delegation depth, so wake -> new thread -> its wake -> new
    thread... is refused at messaging.MAX_DEPTH instead of going on forever."""
    _, _, root = chat(tmp_path)
    state = {"chat": root, "handling": None}  # the first chat answers its user

    def answering(session, message):
        return StructuredCall.objects.create(
            kind="chat_reply",
            session=session,
            user=session.user,
            status="in_progress",
            metadata={"thread_message": str(message.id)} if message else {},
        )

    def wake_and_start_a_thread():
        current, handling = state["chat"], state["handling"]
        call = answering(current, handling)
        wake = wakes.schedule(current, "start the next thread", delay_minutes=1)
        call.delete()
        woken = wakes.fire(wake.id, now=later(2)).thread_message
        assert woken.depth == (handling.depth if handling else 0)
        call = answering(current, woken)  # the wake's turn starts a thread
        thread = root.__class__.objects.create(
            user=root.user, bot_name="kitchen", parent=current
        )
        try:
            state["handling"] = messaging.send(current, thread, "carry on")
        finally:
            call.delete()
        state["chat"] = thread

    for _ in range(messaging.MAX_DEPTH):
        wake_and_start_a_thread()
    with pytest.raises(ValueError, match="Too many hops"):
        wake_and_start_a_thread()


@pytest.mark.django_db(transaction=True)
def test_a_chat_holds_one_pending_wake(tmp_path):
    _, _, session = chat(tmp_path)
    first = wakes.schedule(session, "first", delay_minutes=5)

    with pytest.raises(ValueError, match="already has a wake pending"):
        wakes.schedule(session, "second", delay_minutes=10)
    with pytest.raises(IntegrityError), transaction.atomic():
        Wake.objects.create(
            session=session, bot_name="kitchen", note="raced", due_at=later(5)
        )

    # Another chat has its own.
    other = session.__class__.objects.create(user=session.user, bot_name="kitchen")
    assert wakes.schedule(other, "elsewhere", delay_minutes=5).status == "pending"
    # Once it fires, the chat can schedule the next.
    wakes.fire(first.id, now=later(6))
    assert wakes.schedule(session, "next", delay_minutes=5).chain == 2


@pytest.mark.django_db(transaction=True)
def test_delay_bounds_and_times():
    now = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    la = ZoneInfo("America/Los_Angeles")

    assert wakes.due_time(now, delay_minutes=1) == now + timedelta(minutes=1)
    assert wakes.due_time(now, delay_minutes=24 * 60) == now + timedelta(hours=24)
    for minutes in (0, 0.5, -10):
        with pytest.raises(ValueError, match="at least 1 minute"):
            wakes.due_time(now, delay_minutes=minutes)
    with pytest.raises(ValueError, match="at most 24 hours"):
        wakes.due_time(now, delay_minutes=24 * 60 + 1)
    with pytest.raises(ValueError, match="one of them"):
        wakes.due_time(now)
    with pytest.raises(ValueError, match="one of them"):
        wakes.due_time(now, delay_minutes=5, at="2026-11-07T13:00")
    with pytest.raises(ValueError, match="ISO 8601"):
        wakes.due_time(now, at="in an hour")
    # Without an offset, `at` is in the user's timezone.
    assert wakes.due_time(now, at="2026-11-07T04:30", tz=la) == datetime(
        2026, 11, 7, 12, 30, tzinfo=UTC
    )
    assert wakes.due_time(now, at="2026-11-07T12:30:00+00:00") == datetime(
        2026, 11, 7, 12, 30, tzinfo=UTC
    )


@pytest.mark.django_db(transaction=True)
def test_the_tools_check_their_input(tmp_path):
    from django_ergo.bots.tools import ToolContext

    bot, _, session = chat(tmp_path)
    skill = next(d for d in bot.skill_defs if d.name == "wake")
    assert skill.always
    kit = skill.toolkits(ToolContext(bot=bot, session=session, user=session.user))[0]

    with pytest.raises(ValueError, match="Leave yourself a note"):
        kit.execute_tool("ergo_wake_later", {"note": " ", "delay_minutes": 5})
    with pytest.raises(ValueError, match="at most 24 hours"):
        kit.execute_tool("ergo_wake_later", {"note": "x", "delay_minutes": 2000})
    out = json.loads(
        kit.execute_tool("ergo_wake_later", {"note": "x", "delay_minutes": 30})
    )
    assert (out["in_minutes"], out["chain"]) == (30, 1)
    cancelled = json.loads(kit.execute_tool("ergo_wake_cancel", {}))
    assert cancelled["cancelled"] == 1
    assert json.loads(kit.execute_tool("ergo_wake_cancel", {}))["cancelled"] == 0


@pytest.mark.django_db(transaction=True)
def test_cancel_stops_a_pending_wake_and_one_still_queued(tmp_path):
    _, _, session = chat(tmp_path)
    wake = wakes.schedule(session, "first", delay_minutes=5)

    assert wakes.cancel(session) == 1
    assert wakes.fire_due(later(10)) == []
    wake.refresh_from_db()
    assert wake.status == "cancelled"
    assert not ThreadMessage.objects.exists()

    # Fired while the chat was busy: its message waits in the queue, and cancel stops it.
    queued = wakes.schedule(session, "second", delay_minutes=5)
    wakes.fire(queued.id, now=later(6))
    assert wakes.cancel(session) == 1
    queued.refresh_from_db()
    assert queued.status == "cancelled"
    assert queued.thread_message.status == "failed"
    messaging.deliver(str(queued.thread_message_id))  # a late dispatch: nothing runs
    assert not session.structured_calls.exists()


@pytest.mark.django_db(transaction=True)
def test_a_wake_for_a_busy_chat_waits_its_turn_and_runs_once(tmp_path):
    _, engine, session = chat(tmp_path, "busy", say("Retried."))
    StructuredCall.objects.create(
        kind="chat_reply", session=session, user=session.user, status="in_progress"
    )
    wake = wakes.schedule(session, "retry", delay_minutes=1)

    wakes.fire_due(later(2))
    message = ThreadMessage.objects.get(recipient_session=session)
    messaging.deliver(str(message.id))  # the chat is mid-turn: left queued
    message.refresh_from_db()
    assert message.status == "queued"
    assert wakes.fire_due(later(3)) == []  # and not sent again

    session.structured_calls.update(status="completed")
    messaging.deliver(str(message.id))
    messaging.deliver(str(message.id))  # a duplicate dispatch
    message.refresh_from_db()
    wake.refresh_from_db()
    assert (message.status, message.reply_text) == ("answered", "Retried.")
    assert wake.status == "fired"
    assert session.structured_calls.filter(status="completed").count() == 2
    assert len(engine._client.calls) == 1


@pytest.mark.django_db(transaction=True)
def test_a_failed_wake_is_marked_and_never_retried(tmp_path, monkeypatch):
    _, _, session = chat(tmp_path)

    # Sending fails: the wake is failed and stays failed.
    wake = wakes.schedule(session, "first", delay_minutes=1)

    def broken(*args, **kwargs):
        msg = "database hiccup"
        raise RuntimeError(msg)

    with monkeypatch.context() as patched:
        patched.setattr(messaging, "send_event", broken)
        assert wakes.fire_due(later(2)) == [str(wake.id)]
    wake.refresh_from_db()
    assert (wake.status, wake.error) == ("failed", "database hiccup")
    assert wakes.fire_due(later(3)) == []
    assert not ThreadMessage.objects.exists()

    # Delivery fails (its bot isn't loaded): the wake is failed, the message isn't requeued.
    session.bot_name = "gone"
    session.save(update_fields=["bot_name"])
    wake = wakes.schedule(session, "second", delay_minutes=1)
    wakes.fire_due(later(2))
    messaging.deliver(str(SENT[-1]))
    wake.refresh_from_db()
    assert (wake.status, wake.error) == ("failed", "Bot 'gone' is not loaded")
    assert wake.thread_message.status == "failed"
    assert messaging.redispatch_waiting(older_than=0) == 0
    assert wakes.fire_due(later(5)) == []
