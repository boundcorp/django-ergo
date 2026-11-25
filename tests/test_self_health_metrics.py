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
        tmp_path,
        yaml_text="name: shop\norchestration: true\nself_health: {scope: non_personal}\n",
        name="shop",
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


def test_multiple_native_sessions_suppress_ambiguous_token_totals(scope):
    end = datetime.now(UTC) + timedelta(seconds=1)
    worker = Worker.objects.create(
        session=scope.session, bot_name="shop", title="work", function="agent:orca"
    )
    for native_id in ("one", "two"):
        native = AgentSession.objects.create(
            owner=scope.session.user,
            cli="codex",
            host_namespace="test",
            native_session_id=native_id,
        )
        run = AgentRunSession.objects.create(
            session=native, worker=worker, launch_key=native_id, segment_key=native_id
        )
        AgentUsageEvent.objects.create(
            session=native,
            run_session=run,
            source_event_key="same",
            occurred_at=end - timedelta(seconds=1),
            input_tokens=12,
        )
    usage = report(scope, end.isoformat(), [str(scope.session.pk)])["agent_tokens"]
    assert usage["events"] == 2 and usage["native_sessions"] == 2
    assert usage["input_tokens"] is None


def test_authentication_and_caller_access_are_required(scope):
    from django.contrib.auth.models import AnonymousUser

    end = datetime.now(UTC).isoformat()
    scope.user = AnonymousUser()
    with pytest.raises(ValueError, match="authenticated authorized"):
        report(scope, end, [str(scope.session.pk)])
    scope.user = scope.session.user
    scope.bot.definition.allowed_users = ["someone-else"]
    with pytest.raises(ValueError, match="authenticated authorized"):
        report(scope, end, [str(scope.session.pk)])


def test_partial_token_fields_are_unknown(scope):
    end = datetime.now(UTC) + timedelta(seconds=1)
    worker = Worker.objects.create(
        session=scope.session, bot_name="shop", title="work", function="agent:orca"
    )
    native = AgentSession.objects.create(
        owner=scope.session.user,
        cli="codex",
        host_namespace="test",
        native_session_id="partial",
    )
    run = AgentRunSession.objects.create(
        session=native, worker=worker, launch_key="partial", segment_key="partial"
    )
    for key, value in (("one", 12), ("two", None)):
        AgentUsageEvent.objects.create(
            session=native,
            run_session=run,
            source_event_key=key,
            occurred_at=end - timedelta(seconds=1),
            input_tokens=value,
        )
    usage = report(scope, end.isoformat(), [str(scope.session.pk)])["agent_tokens"]
    assert usage["events"] == 2 and usage["known_input"] == 1
    assert usage["input_tokens"] is None


def _session(user, bot_name="shop", created=None):
    from django_ergo.conversation.models import ConversationSession

    session = ConversationSession.objects.create(
        user=user,
        bot_name=bot_name,
        engine_type="claude",
        transport_type="api",
        status="active",
    )
    if created:
        ConversationSession.objects.filter(pk=session.pk).update(created_at=created)
    return session


def _message(session, when, sequence=1):
    message = SessionMessage.objects.create(
        session=session, role="user", sequence=sequence
    )
    SessionMessage.objects.filter(pk=message.pk).update(created_at=when)


def _discovery(scope, end=None):
    end = end or datetime.now(UTC) + timedelta(seconds=1)
    return report(scope, end.isoformat())["scope"]["discovery"]


def test_classification_defaults_exclude_unknown(tmp_path):
    from django_ergo.bots.definition import BotDefinitionError
    from django_ergo.bots.definition import SelfHealth
    from django_ergo.bots.self_health import classify

    bot, _ = make_bot(tmp_path, yaml_text="name: plain\n", name="plain")
    sid = "00000000-0000-0000-0000-000000000001"
    assert bot.definition.self_health == SelfHealth()
    assert classify(bot, sid) == (False, "unclassified")
    bot.definition.self_health = SelfHealth(include_sessions=(sid,))
    assert classify(bot, sid) == (True, "session_allowed")
    bot.definition.self_health = SelfHealth("non_personal", (sid,), (sid,))
    assert classify(bot, sid) == (False, "denied_session")
    bot.definition.self_health = SelfHealth("personal", (sid,))
    assert classify(bot, sid) == (False, "personal_bot")
    bot.definition.self_health = SelfHealth("non_personal")
    bot.definition.private = True
    assert classify(bot, sid) == (False, "private_bot")
    from django_ergo.bots.definition import BotDefinition

    for bad in ({"scope": "maybe"}, {"extra": 1}, {"include_sessions": ["x"]}, []):
        with pytest.raises(BotDefinitionError):
            BotDefinition.from_dict({"name": "x", "self_health": bad})


def test_names_never_classify(scope):
    from django_ergo.bots.definition import SelfHealth
    from django_ergo.bots.self_health import classify

    scope.bot.definition.self_health = SelfHealth()
    scope.bot.definition.name = "public-team-chat"
    assert classify(scope.bot, scope.session.pk) == (False, "unclassified")


def test_automatic_discovery_matches_explicit_and_excludes_unclassified(scope):
    now = datetime.now(UTC)
    end = now + timedelta(seconds=1)
    _message(scope.session, now)
    quiet = _session(scope.user)
    assert str(quiet.pk) not in str(_discovery(scope, end))
    auto = report(scope, end.isoformat())
    assert auto["scope"]["selection"] == "automatic"
    assert auto["scope"]["discovery"]["included"] == 1
    explicit = report(scope, end.isoformat(), [str(scope.session.pk)])
    assert explicit["scope"]["selection"] == "explicit"
    for key in ("inventory", "workers", "wakes", "tools", "agent_tokens", "window"):
        if key == "inventory":
            assert auto[key]["sessions"] == explicit[key]["sessions"]
        else:
            assert auto[key] == explicit[key]
    assert report(scope, end.isoformat(), []) == auto
    scope.bot.definition.self_health = type(scope.bot.definition.self_health)()
    result = report(scope, end.isoformat())
    assert result["scope"]["discovery"]["excluded_by_reason"] == {"unclassified": 1}
    assert result["inventory"]["sessions"] == 0
    with pytest.raises(ValueError, match="not classified"):
        report(scope, end.isoformat(), [str(scope.session.pk)])


def test_discovery_excludes_personal_private_denied_and_other_users(scope):
    from django_ergo.bots.definition import SelfHealth

    now = datetime.now(UTC)
    end = now + timedelta(seconds=1)
    other = get_user_model().objects.create(username="stranger")
    _message(scope.session, now)
    foreign = _session(other)
    _message(foreign, now)
    denied = _session(scope.user)
    _message(denied, now)
    scope.bot.definition.self_health = SelfHealth(
        "non_personal", exclude_sessions=(str(denied.pk),)
    )
    found = _discovery(scope, end)
    assert found["included"] == 1
    assert found["excluded_by_reason"] == {"denied_session": 1}
    assert found["active_sessions_considered"] == 2  # other user's never considered
    with pytest.raises(ValueError, match="inaccessible"):
        report(scope, end.isoformat(), [str(foreign.pk)])
    with pytest.raises(ValueError, match="inaccessible"):
        report(scope, end.isoformat(), [str(denied.pk)])
    scope.bot.definition.self_health = SelfHealth("personal")
    assert _discovery(scope, end)["excluded_by_reason"] == {"personal_bot": 2}
    scope.bot.definition.self_health = SelfHealth("non_personal")
    scope.bot.definition.private = True
    found = _discovery(scope, end)
    assert found["included"] == 0 and found["excluded_by_reason"] == {"private_bot": 2}


def test_session_allow_list_and_out_of_scope_bot(scope):
    from django_ergo.bots.definition import SelfHealth

    now = datetime.now(UTC)
    end = now + timedelta(seconds=1)
    scope.bot.definition.self_health = SelfHealth(
        include_sessions=(str(scope.session.pk),)
    )
    _message(scope.session, now)
    stray = _session(scope.user, bot_name="not-visible")
    _message(stray, now)
    found = _discovery(scope, end)
    assert found["included"] == 1
    assert found["excluded_by_reason"] == {"out_of_scope": 1}
    assert "not-visible" not in str(found)


def test_window_boundaries_for_discovery(scope):
    end = datetime.now(UTC).replace(microsecond=0)
    start = end - timedelta(days=7)
    inside = _session(scope.user, created=start - timedelta(days=1))
    _message(inside, start)
    at_end = _session(scope.user, created=start - timedelta(days=1))
    _message(at_end, end)
    before = _session(scope.user, created=start - timedelta(days=1))
    _message(before, start - timedelta(microseconds=1))
    # scope.session itself has no activity in the window
    found = _discovery(scope, end)
    assert found["included"] == 1
    explicit = report(scope, end.isoformat(), [str(inside.pk)])
    assert report(scope, end.isoformat())["inventory"]["sessions"] == 1
    assert explicit["inventory"]["sessions"] == 1


def test_cap_truncation_is_reported_and_deterministic(scope, monkeypatch):
    from django_ergo.bots import self_health

    monkeypatch.setattr(self_health, "MAX_SESSIONS", 2)
    now = datetime.now(UTC)
    end = now + timedelta(seconds=1)
    made = [
        _session(scope.user, created=now - timedelta(hours=3 - n)) for n in range(3)
    ]
    for item in made:
        _message(item, now)
    first = report(scope, end.isoformat())
    assert first["scope"]["discovery"]["truncated"] is True
    assert first["scope"]["discovery"]["included"] == 2
    assert first["scope"]["discovery"]["not_included_due_to_cap"] == 1
    assert "truncated" in first["scope"]["coverage"]
    assert report(scope, end.isoformat()) == first
    # oldest two by creation time are kept
    kept = report(scope, end.isoformat(), [str(made[0].pk), str(made[1].pk)])
    assert kept["workers"] == first["workers"]
    assert kept["inventory"]["sessions"] == first["inventory"]["sessions"] == 2


def test_discovery_returns_no_content(scope):
    now = datetime.now(UTC)
    message = SessionMessage.objects.create(
        session=scope.session, role="user", sequence=1
    )
    block = MessageBlock.objects.create(
        message=message, sequence=1, block_type="text", text="secret chat words"
    )
    MessageBlock.objects.filter(pk=block.pk).update(created_at=now)
    SessionMessage.objects.filter(pk=message.pk).update(created_at=now)
    result = report(scope, (now + timedelta(seconds=1)).isoformat())
    assert "secret chat words" not in str(result)


def test_explicit_work_assertions_are_scoped_and_separate_from_transport(scope):
    end = datetime.now(UTC) + timedelta(seconds=1)
    for value in (
        "completed",
        "partial",
        "blocked",
        "failed",
        None,
        [],
        True,
        "invented",
    ):
        Worker.objects.create(
            session=scope.session,
            bot_name="shop",
            title="work",
            function="agent:orca",
            status="completed",
            completed_at=end - timedelta(seconds=1),
            result={"work_disposition": value, "report": "completed successfully"},
        )
    # Missing evidence, prose and a transport status alone remain unknown.
    Worker.objects.create(
        session=scope.session,
        bot_name="shop",
        title="legacy",
        function="agent:orca",
        status="completed",
        completed_at=end - timedelta(seconds=1),
        result="Work completed",
    )
    foreign = _session(get_user_model().objects.create(username="foreign-assertion"))
    Worker.objects.create(
        session=foreign,
        bot_name="shop",
        title="foreign",
        function="agent:orca",
        status="completed",
        completed_at=end - timedelta(seconds=1),
        result={"work_disposition": "completed"},
    )
    result = report(scope, end.isoformat(), [str(scope.session.pk)])["workers"]
    assert result["results_by_current_status"] == {"completed": 9}
    assert result["work_assertions"]["counts"] == {
        "completed": 1,
        "partial": 1,
        "blocked": 1,
        "failed": 1,
        "unknown": 5,
    }
