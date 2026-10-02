"""Resuming turns that failed (see tasks.resume_session and the resume_failed command)."""

import pytest
from django.core.management import call_command
from django.db import transaction
from django.test.utils import override_settings
from django_ergo.bots.messaging import turn_text
from django_ergo.conversation.models import ConversationSession, StructuredCall, ThreadMessage

from ergonaut.apps.bots import tasks
from ergonaut.apps.bots.errors import describe_error
from ergonaut.apps.bots.tests.fakes import say
from ergonaut.apps.bots.tests.test_api import bot_folder, cook, post, use_bots  # noqa: F401

QUOTA = (
    "RateLimitError: Error code: 429 - {'error': {'message': 'You exceeded your current quota, "
    "please check your plan and billing details.', 'type': 'insufficient_quota'}}"
)


def test_errors_read_in_plain_words():
    assert describe_error(QUOTA)["title"] == "Out of API credits"
    assert describe_error("RateLimitError: Error code: 429 - slow down")["kind"] == "rate_limit"
    assert describe_error("AuthenticationError: Error code: 401 - invalid_api_key")["kind"] == "auth"
    assert describe_error("The turn stopped without finishing (its worker exited).")["kind"] == "worker"
    other = describe_error("ValueError: something odd\nTraceback...")
    assert (other["kind"], other["title"]) == ("other", "ValueError: something odd")


@pytest.fixture
def session(cook):  # noqa: F811
    return ConversationSession.objects.create(user=cook, bot_name="kitchen", metadata={"bot_role": "main"})


def fail(session, **metadata):
    return StructuredCall.objects.create(
        kind="chat_reply", session=session, user=session.user, status="failed", error=QUOTA, metadata=metadata
    )


@pytest.mark.django_db(transaction=True)
def test_resume_carries_on_a_failed_turn(session, client, use_bots):  # noqa: F811
    use_bots(say("Picking up where I left off."))
    fail(session)
    detail = client.get(f"/api/sessions/{session.id}").json()
    assert detail["calls"][-1]["problem"]["title"] == "Out of API credits"

    response = post(client, f"/api/sessions/{session.id}/resume")
    assert response.status_code == 200
    latest = session.structured_calls.order_by("-created_at").first()
    assert latest.status == "completed"
    assert latest.request.startswith("[Resuming: your last turn stopped with an error")
    assert "Out of API credits" in latest.request
    # Nothing failed now: a second press has nothing to resume.
    assert post(client, f"/api/sessions/{session.id}/resume").status_code == 409


@pytest.mark.django_db(transaction=True)
def test_resuming_a_handoff_turn_runs_it_again_on_that_message(session, cook):  # noqa: F811
    boss = ConversationSession.objects.create(user=cook, bot_name="boss", metadata={"bot_role": "main"})
    message = ThreadMessage.objects.create(
        sender_session=boss,
        recipient_session=session,
        text="Design the cover",
        status="answered",
        reply_text="(no reply: out of credits)",
    )
    fail(session, thread_message=str(message.id))
    sent = []
    with override_settings(DJANGO_ERGO={"THREAD_MESSAGE_RUNNER": sent.append}):
        with transaction.atomic():
            assert tasks.resume_session(session) is True
    message.refresh_from_db()
    assert (message.status, message.reply_text, message.metadata["resumed"]) == ("queued", "", True)
    assert sent == [str(message.id)]
    assert turn_text(message).startswith("[Resuming: your turn on the message from boss · Main")
    assert tasks.resume_session(session) is None  # once only


@pytest.mark.django_db(transaction=True)
def test_resume_failed_resumes_every_chat_out_of_credits(session, cook, use_bots, capsys):  # noqa: F811
    use_bots(say("Back."), say("Back too."))
    other = ConversationSession.objects.create(user=cook, bot_name="kitchen", metadata={"bot_role": "thread"})
    crashed = ConversationSession.objects.create(user=cook, bot_name="kitchen", metadata={"bot_role": "thread"})
    fail(session)
    fail(other)
    StructuredCall.objects.create(
        kind="chat_reply", session=crashed, user=cook, status="failed", error="ValueError: odd"
    )

    call_command("resume_failed", "--kind", "credits", "--dry-run")
    assert "would resume" in capsys.readouterr().out
    assert StructuredCall.objects.filter(status="completed").count() == 0

    call_command("resume_failed", "--kind", "credits")
    assert "resumed 2" in capsys.readouterr().out
    assert StructuredCall.objects.filter(status="completed").count() == 2
    assert crashed.structured_calls.count() == 1  # not a credits failure
