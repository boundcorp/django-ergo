from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model

from django_ergo.bots.self_health import report
from django_ergo.bots.tools import ToolContext
from django_ergo.conversation.models import AgentRunSession
from django_ergo.conversation.models import AgentSession
from django_ergo.conversation.models import AgentUsageEvent
from django_ergo.conversation.models import MessageBlock
from django_ergo.conversation.models import SessionMessage
from django_ergo.conversation.models import Wake
from django_ergo.conversation.models import Worker
from tests.test_bots import make_bot

pytestmark = pytest.mark.django_db


@pytest.fixture
def scope(tmp_path):
    bot, _ = make_bot(
        tmp_path, yaml_text="name: shop\norchestration: true\n", name="shop"
    )
    user = get_user_model().objects.create(username="health-user")
    session = async_to_sync(bot.main_session)(user)
    return ToolContext(bot=bot, session=session, user=user)


def test_empty_default_and_access_checks(scope):
    end = datetime.now(UTC)
    assert report(scope, end.isoformat())["inventory"]["sessions"] == 0
    with pytest.raises(ValueError, match="timezone"):
        report(scope, "2026-01-01T00:00:00")
    with pytest.raises(ValueError, match="inaccessible"):
        report(scope, end.isoformat(), ["00000000-0000-0000-0000-000000000000"])
    with pytest.raises(ValueError, match="200"):
        report(scope, end.isoformat(), [str(scope.session.pk)] * 201)


def test_window_counts_and_native_usage_without_payloads(scope):
    end = datetime.now(UTC) + timedelta(seconds=1)
    start = end - timedelta(days=7)
    session = scope.session
    worker = Worker.objects.create(
        session=session,
        bot_name="shop",
        title="private title",
        function="agent:orca",
        status="failed",
        completed_at=end - timedelta(seconds=1),
    )
    Worker.objects.filter(pk=worker.pk).update(created_at=start)
    late = Worker.objects.create(
        session=session, bot_name="shop", title="late", function="task:x"
    )
    Worker.objects.filter(pk=late.pk).update(created_at=end)
    Wake.objects.create(
        session=session,
        bot_name="shop",
        note="secret wake",
        due_at=start,
        status="fired",
        fired_at=start,
    )
    message = SessionMessage.objects.create(session=session, role="user", sequence=1)
    block = MessageBlock.objects.create(
        message=message,
        sequence=1,
        block_type="tool_result",
        is_error=True,
        tool_result_content="secret payload",
    )
    MessageBlock.objects.filter(pk=block.pk).update(created_at=start)
    native = AgentSession.objects.create(
        owner=session.user, cli="codex", host_namespace="test", native_session_id="one"
    )
    run = AgentRunSession.objects.create(
        session=native, worker=worker, launch_key="one", segment_key="one"
    )
    AgentUsageEvent.objects.create(
        session=native,
        run_session=run,
        source_event_key="one",
        occurred_at=start,
        input_tokens=12,
        output_tokens=None,
    )
    AgentUsageEvent.objects.create(
        session=native,
        run_session=run,
        source_event_key="outside",
        occurred_at=end,
        input_tokens=99,
    )
    result = report(scope, end.isoformat(), [str(session.pk), str(session.pk)])
    assert result == report(scope, end.isoformat(), [str(session.pk)])
    assert result["inventory"]["sessions"] == 1
    assert result["workers"]["attempts_started"] == 1
    assert result["workers"]["results_in_window"] == 1
    assert result["wakes"]["one_shot_fired"] == 1
    assert (
        result["tools"]["recorded_failures"] == result["tools"]["recorded_results"] == 1
    )
    assert result["agent_tokens"]["input_tokens"] == 12
    assert result["agent_tokens"]["output_tokens"] is None
    assert result["agent_tokens"]["events"] == 1
    assert "secret" not in str(result) and "private title" not in str(result)


def test_rejects_other_user_and_private_bots(scope):
    from django_ergo.conversation.models import ConversationSession

    end = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    other = get_user_model().objects.create(username="other-health-user")
    foreign = ConversationSession.objects.create(
        user=other,
        bot_name="shop",
        engine_type="claude",
        transport_type="api",
        status="active",
    )
    with pytest.raises(ValueError, match="inaccessible"):
        report(scope, end, [str(foreign.pk)])
    scope.bot.definition.private = True
    with pytest.raises(ValueError, match="inaccessible"):
        report(scope, end, [str(scope.session.pk)])


def test_recurring_wake_count_uses_dispatch_time(scope):
    from django_ergo.conversation.models import RecurringWake
    from django_ergo.conversation.models import RecurringWakeOccurrence

    end = datetime.now(UTC) + timedelta(seconds=1)
    start = end - timedelta(days=7)
    schedule = RecurringWake.objects.create(
        session=scope.session,
        bot_name="shop",
        note="sensitive-note-value",
        interval_seconds=60,
        next_run_at=end,
        max_occurrences=4,
    )
    RecurringWakeOccurrence.objects.create(
        schedule=schedule, scheduled_for=start, dispatched_at=start, status="answered"
    )
    RecurringWakeOccurrence.objects.create(
        schedule=schedule, scheduled_for=end, dispatched_at=end, status="answered"
    )
    result = report(scope, end.isoformat(), [str(scope.session.pk)])
    assert result["wakes"]["recurring_dispatched"] == 1
    assert "sensitive-note-value" not in str(result)
