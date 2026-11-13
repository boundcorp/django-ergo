import csv
import io
import json
from datetime import UTC, datetime, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, connection
from django_ergo.conversation.models import ConversationSession, StructuredCall, ThreadMessage

from ergonaut.apps.bots.management.commands.voice_audit import collect
from ergonaut.apps.bots.models import RealtimeVoiceDelegation, RealtimeVoiceEvent, RealtimeVoiceSession


@pytest.mark.django_db(transaction=True)
def test_owner_window_redaction_and_exact_call_correlation():
    owner = get_user_model().objects.create_user("audit-owner")
    other = get_user_model().objects.create_user("audit-other")
    chat = ConversationSession.objects.create(user=owner)
    other_chat = ConversationSession.objects.create(user=other)
    start = datetime(2026, 11, 1, tzinfo=UTC)
    selected = RealtimeVoiceSession.objects.create(user=owner, chat=chat, state={"secret": "private-state"})
    excluded = RealtimeVoiceSession.objects.create(user=owner, chat=chat)
    RealtimeVoiceSession.objects.filter(pk=selected.pk).update(created_at=start)
    RealtimeVoiceSession.objects.filter(pk=excluded.pk).update(created_at=start + timedelta(days=1))
    RealtimeVoiceSession.objects.create(user=other, chat=other_chat)
    RealtimeVoiceSession.objects.create(user=owner, chat=other_chat)
    RealtimeVoiceEvent.objects.create(
        session=selected, event_id="private-event", kind="user", text="private-transcript"
    )
    message = ThreadMessage.objects.create(
        recipient_session=chat, text="private-task", reply_text="private-reply", error="private-error"
    )
    RealtimeVoiceDelegation.objects.create(
        session=selected, message=message, event_id="private-delegation", text="private-request"
    )
    StructuredCall.objects.create(
        session=chat, metadata={"thread_message": str(message.pk)}, model_name="gpt-6-luna", input_tokens=19
    )
    StructuredCall.objects.create(session=chat, model_name="unrelated-model", input_tokens=123)
    report = collect(owner.username, start, start + timedelta(days=1))
    assert len(report["sessions"]) == 1
    row = report["sessions"][0]
    assert row["ended_at"] is None
    assert row["provider_usage_seconds"] is None
    assert row["invoice_charged_usd"] is None
    assert row["delegations"][0]["calls"][0]["recorded_aggregate_tokens"]["input_tokens"] == 19
    serialized = json.dumps(report)
    for private in ("private-", str(selected.pk), str(chat.pk), str(message.pk), owner.username, "unrelated-model"):
        assert private not in serialized

    # The database enforces read-only, not merely a promise in the command's help.
    class AttemptWrite:
        def __call__(self, execute, sql, params, many, context):
            if sql.lstrip().startswith("SELECT") and "ergonaut_bots_realtimevoicesession" in sql:
                with connection.cursor() as cursor:
                    cursor.execute("UPDATE ergonaut_bots_realtimevoicesession SET status = 'ended'")
            return execute(sql, params, many, context)

    with connection.execute_wrapper(AttemptWrite()), pytest.raises(DatabaseError, match="read-only transaction"):
        collect(owner.username)
    selected.refresh_from_db()
    assert selected.status == "active"


@pytest.mark.django_db(transaction=True)
def test_csv_unknowns_and_invalid_time_windows():
    owner = get_user_model().objects.create_user("audit-csv")
    chat = ConversationSession.objects.create(user=owner)
    RealtimeVoiceSession.objects.create(user=owner, chat=chat, status="=malicious")
    stream = io.StringIO()
    call_command("voice_audit", owner=owner.username, format="csv", stdout=stream)
    row = next(csv.DictReader(io.StringIO(stream.getvalue())))
    assert row["status"] == "UNKNOWN"
    assert row["created_to_ended_seconds"] == "UNKNOWN"
    assert row["voice_audio_text_tokens"] == "UNKNOWN"
    for bounds in (
        {"since": "2026-11-01"},
        {"since": "bad"},
        {"since": "2026-11-02T00:00:00Z", "until": "2026-11-01T00:00:00Z"},
    ):
        with pytest.raises(CommandError):
            call_command("voice_audit", owner=owner.username, stdout=io.StringIO(), **bounds)
