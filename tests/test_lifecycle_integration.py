import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.utils import timezone

from django_ergo.bots import messaging
from django_ergo.bots import workers
from django_ergo.conversation.lifecycle_report import project_lifecycle
from django_ergo.conversation.models import ThreadMessage
from django_ergo.models import LifecycleEvent
from tests.test_bot_workers import TOOLS
from tests.test_bot_workers import YAML
from tests.test_bots import claude_tool
from tests.test_bots import make_bot
from tests.test_bots import say


@pytest.mark.django_db(transaction=True)
def test_worker_result_turn_consumption_and_handoff(tmp_path, settings):
    settings.DJANGO_ERGO = {
        "LIFECYCLE_EVENTS_ENABLED": True,
        "WORKER_RUNNER": "tests.test_bot_workers.record_step",
        "THREAD_MESSAGE_RUNNER": "tests.test_bots.record_message",
    }
    bot, _ = make_bot(tmp_path, say("Synthetic followup"), yaml_text=YAML, tools=TOOLS)
    user = get_user_model().objects.create(username="synthetic-worker")
    session = async_to_sync(bot.main_session)(user)
    worker = workers.start(bot, session, "watch", {"target": "synthetic", "polls": 1})
    assert workers.run(str(worker.pk), bot.registry) == "completed"
    message = ThreadMessage.objects.get(metadata__worker=str(worker.pk))
    before = project_lifecycle(LifecycleEvent.objects.all(), as_of=timezone.now())
    assert before["workers"][0]["result_status"] == "result_unconsumed"
    messaging.deliver(str(message.pk), registry=bot.registry)
    after = project_lifecycle(LifecycleEvent.objects.all(), as_of=timezone.now())
    assert after["workers"][0]["result_status"] == "followup_consumed"
    stages = {
        row["stage"]
        for row in after["timeline"]
        if row["message_id"] == str(message.pk)
    }
    assert {"queued", "dispatched", "delivered", "started", "resumed"} <= stages
    assert "Synthetic followup" not in repr(after)


@pytest.mark.django_db(transaction=True)
def test_approval_recorded_without_inventing_surfacing_or_human_owner(
    tmp_path, settings
):
    settings.DJANGO_ERGO = {"LIFECYCLE_EVENTS_ENABLED": True}
    user = get_user_model().objects.create(username="synthetic-approval")
    bot, _ = make_bot(
        tmp_path,
        claude_tool("add_to_list", {"item": "synthetic", "qty": 2}, tool_id="t1"),
        say("Done"),
    )
    session = async_to_sync(bot.root_session)(user)
    paused = async_to_sync(bot.ask)(session, "synthetic request")
    assert paused.needs_approval
    events = list(LifecycleEvent.objects.filter(category="approval"))
    assert [e.stage for e in events] == ["blocker_created"]
    assert events[0].actor == events[0].state == "unknown"
    async_to_sync(bot.resume)(session, {"t1": True})
    events = list(
        LifecycleEvent.objects.filter(category="approval").order_by("observed_at")
    )
    assert [e.stage for e in events] == [
        "blocker_created",
        "answer_received",
        "resumed",
        "blocker_resolved",
    ]
    assert len({e.question_id for e in events}) == 1
    report = project_lifecycle(events, as_of=timezone.now())
    blocker = report["tasks"][0]["blockers"][0]
    assert blocker["status"] == "answered_resumed"
    assert blocker["human_wait_seconds"] is None
    assert "synthetic request" not in repr(report)


@pytest.mark.django_db(transaction=True)
def test_cross_thread_handoff_and_reply_are_correlated(tmp_path, settings):
    settings.DJANGO_ERGO = {
        "LIFECYCLE_EVENTS_ENABLED": True,
        "THREAD_MESSAGE_RUNNER": "tests.test_bots.record_message",
    }
    bot, _ = make_bot(tmp_path, say("Synthetic answer"), say("Synthetic followup"))
    user = get_user_model().objects.create(username="synthetic-handoff")
    root = async_to_sync(bot.root_session)(user)
    thread = async_to_sync(bot.create_session)(user, parent=root, title="Synthetic")
    message = messaging.send(root, thread, "Synthetic request", registry=bot.registry)
    messaging.deliver(str(message.pk), registry=bot.registry)
    reply = ThreadMessage.objects.get(in_reply_to=message)
    messaging.deliver(str(reply.pk), registry=bot.registry)
    report = project_lifecycle(LifecycleEvent.objects.all(), as_of=timezone.now())
    assert {"task_id": str(reply.pk), "related_task_id": str(message.pk)} in report[
        "related_tasks"
    ]
    for item in (message, reply):
        stages = {
            r["stage"] for r in report["timeline"] if r["task_id"] == str(item.pk)
        }
        assert {
            "queued",
            "dispatched",
            "delivered",
            "started",
            "resumed",
            "result_persisted",
        } <= stages
