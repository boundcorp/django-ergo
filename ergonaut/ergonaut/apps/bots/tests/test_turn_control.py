"""Stopping, steering and interrupting a running turn (see ergonaut.apps.bots.tasks)."""

import pytest
from django_ergo.conversation.models import ConversationSession

from ergonaut.apps.bots import tasks
from ergonaut.apps.bots.tests.fakes import say, tool_call
from ergonaut.apps.bots.tests.test_api import bot_folder, cook, post, use_bots  # noqa: F401


def during_first_call(client, action):
    """Run ``action`` while the model makes its first call of the turn (a tool is next)."""
    create = client.create

    async def wrapped(**kwargs):
        if len(client.calls) == 0:
            action()
        return await create(**kwargs)

    client.create = wrapped


def texts(messages):
    return [b.get("text") for m in messages if isinstance(m["content"], list) for b in m["content"]]


@pytest.fixture
def session(cook):  # noqa: F811
    return ConversationSession.objects.create(user=cook, bot_name="kitchen", metadata={"bot_role": "root"})


@pytest.mark.django_db(transaction=True)
def test_messages_waiting_in_the_inbox_are_answered_together(session, use_bots):  # noqa: F811
    use_bots(say("Tacos, and yes."))
    tasks.push_message(session.id, "Dinner?")
    tasks.push_message(session.id, "Also, any eggs?")
    tasks.run_turn(str(session.id))
    call = session.structured_calls.get()
    assert (call.status, call.request) == ("completed", "Dinner?\n\nAlso, any eggs?")
    assert not tasks.inbox_waiting(session.id)

    tasks.run_turn(str(session.id))  # nothing waiting: a no-op
    assert session.structured_calls.count() == 1


@pytest.mark.django_db(transaction=True)
def test_a_message_sent_mid_turn_steers_it(session, use_bots):  # noqa: F811
    client = use_bots(tool_call("pantry_count", {"item": "eggs"}), say("Four eggs, no milk."))
    during_first_call(client, lambda: tasks.push_message(session.id, "And milk?"))
    tasks.push_message(session.id, "How many eggs?")
    tasks.run_turn(str(session.id))

    call = session.structured_calls.get()
    assert call.status == "completed"
    second = client.calls[1]["messages"]
    assert texts(second[-1:]) == ["And milk?"]
    assert "How many eggs?" in texts(second)  # same turn: the native window kept it


@pytest.mark.django_db(transaction=True)
def test_stop_ends_the_turn_after_its_running_tool(session, use_bots):  # noqa: F811
    client = use_bots(tool_call("pantry_count", {"item": "eggs"}), say("never sent"))
    stopped = []
    during_first_call(client, lambda: stopped.append(tasks.request_stop(session.id)))
    tasks.push_message(session.id, "How many eggs?")
    tasks.run_turn(str(session.id))

    assert stopped == [True]
    call = session.structured_calls.get()
    assert (call.status, call.error) == ("stopped", "Stopped by the user")
    assert len(client.calls) == 1
    assert not tasks.stop_requested(session.id)  # nothing else runs; the next turn starts clean
    # With nothing running, stop is a no-op.
    assert tasks.request_stop(session.id) is False
    assert not tasks.stop_requested(session.id)


@pytest.mark.django_db(transaction=True)
def test_interrupt_stops_the_turn_and_answers_the_new_message(session, use_bots):  # noqa: F811
    client = use_bots(tool_call("pantry_count", {"item": "eggs"}), say("Pasta it is."))

    def interrupt():
        tasks.request_stop(session.id)
        tasks.push_message(session.id, "Actually, make pasta")

    during_first_call(client, interrupt)
    tasks.push_message(session.id, "How many eggs?")
    tasks.run_turn(str(session.id))

    calls = list(session.structured_calls.order_by("created_at"))
    assert [(c.status, c.request) for c in calls] == [
        ("stopped", "How many eggs?"),
        ("completed", "Actually, make pasta"),
    ]
    # The new turn continues from where the stopped one left off.
    assert "How many eggs?" in texts(client.calls[1]["messages"])


@pytest.mark.django_db(transaction=True)
def test_stop_and_interrupt_endpoints(client, cook, use_bots):  # noqa: F811
    use_bots(say("Tacos."))
    root = post(client, "/api/bots/kitchen/root").json()
    stop = post(client, f"/api/sessions/{root['id']}/stop")
    assert stop.status_code == 200
    assert stop.json()["queued"] is False  # nothing was running

    # With nothing running, an interrupt is just a message.
    turn = post(client, f"/api/sessions/{root['id']}/messages", {"text": "Dinner?", "mode": "interrupt"})
    assert turn.status_code == 200, turn.content
    assert turn.json()["text"] == "Tacos."
    assert post(client, f"/api/sessions/{root['id']}/messages", {"text": "x", "mode": "later"}).status_code == 422

    # Someone else's session can't be stopped.
    from django.contrib.auth import get_user_model

    someone = get_user_model().objects.create_user("someone", "s@example.com", "pw")
    other = ConversationSession.objects.create(user=someone, bot_name="kitchen", metadata={"bot_role": "root"})
    assert post(client, f"/api/sessions/{other.id}/stop").status_code == 404


@pytest.mark.django_db(transaction=True)
def test_a_turn_answering_a_bot_takes_steering_and_its_follow_up(session, cook, use_bots):  # noqa: F811
    import threading

    from django_ergo.bots import messaging
    from django_ergo.conversation.models import ThreadMessage

    boss = ConversationSession.objects.create(user=cook, bot_name="kitchen", metadata={"bot_role": "main"})
    client = use_bots(tool_call("pantry_count", {"item": "eggs"}), say("Four eggs, no milk."))
    request = ThreadMessage.objects.create(sender_session=boss, recipient_session=session, text="How many eggs?")

    def mid_turn():
        tasks.push_message(session.id, "From the user: and milk?")
        # The boss adds to its handoff while it's being worked on: it steers, no second turn.
        sent = threading.Thread(target=lambda: messaging.send(boss, session, "Butter too."))
        sent.start()
        sent.join()

    during_first_call(client, mid_turn)
    messaging.deliver(str(request.id))  # as a worker does, holding the turn lock

    # Both arrived during the same step, so they reach the model as one message.
    assert texts(client.calls[1]["messages"][-1:]) == [
        "From the user: and milk?\n\n[More from kitchen · Main "
        f"(thread {boss.id}), adding to its message you're working on]\n\nButter too."
    ]
    assert ThreadMessage.objects.filter(recipient_session=session).count() == 1
    request.refresh_from_db()
    assert request.status == "answered"
    assert request.text == "How many eggs?\n\nButter too."
    assert session.structured_calls.count() == 1


@pytest.mark.django_db(transaction=True)
def test_a_message_the_turn_hasnt_read_can_be_unsent(session, client, use_bots):  # noqa: F811
    use_bots(tool_call("pantry_count", {"item": "eggs"}), say("Four eggs."))
    first = tasks.push_message(session.id, "Oops, wrong chat")
    tasks.push_message(session.id, "How many eggs?")
    detail = client.get(f"/api/sessions/{session.id}").json()
    assert [m["text"] for m in detail["inbox"]] == ["Oops, wrong chat", "How many eggs?"]

    response = client.delete(f"/api/sessions/{session.id}/inbox/{first}")
    assert response.status_code == 200
    assert response.json() == {"text": "Oops, wrong chat"}
    assert client.delete(f"/api/sessions/{session.id}/inbox/{first}").status_code == 409  # already gone

    tasks.run_turn(str(session.id))
    call = session.structured_calls.get()
    assert call.request == "How many eggs?"
    assert client.get(f"/api/sessions/{session.id}").json()["inbox"] == []
