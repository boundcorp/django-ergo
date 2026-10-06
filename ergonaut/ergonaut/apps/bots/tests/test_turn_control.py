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


def delegated(session):
    """A request from another chat, queued for ``session``."""
    from django_ergo.conversation.models import ThreadMessage

    sender = ConversationSession.objects.create(
        user=session.user, bot_name="kitchen", metadata={"bot_role": "thread", "title": "Planner"}
    )
    return ThreadMessage.objects.create(sender_session=sender, recipient_session=session, text="Count the eggs")


@pytest.fixture
def held_messages(settings):
    """Thread messages are recorded, not delivered: a test runs only the turns it starts
    (a delivered reply would start a turn of the sender's, on the test's scripted model)."""
    held = []
    settings.DJANGO_ERGO = {**settings.DJANGO_ERGO, "THREAD_MESSAGE_RUNNER": held.append}
    return held


@pytest.mark.django_db(transaction=True)
def test_the_user_can_steer_and_stop_a_turn_another_chat_started(session, use_bots, held_messages):  # noqa: F811
    from django_ergo.bots import messaging, webhooks

    client = use_bots(tool_call("pantry_count", {"item": "eggs"}), say("Four eggs, no milk."))
    during_first_call(client, lambda: tasks.push_message(session.id, "And milk?"))
    messaging.deliver(str(delegated(session).id), registry=webhooks.get_registry())

    second = client.calls[1]["messages"]
    assert "And milk?" in texts(second[-1:])[0]
    assert "[Message from" not in texts(second[-1:])[0]
    steering = session.messages.get(content_blocks__text__contains="And milk?")
    assert steering.author["kind"] == "django_user"
    assert steering.author["ref"] == str(session.user_id)
    assert steering.provenance == {}
    assert not tasks.inbox_waiting(session.id)  # taken by this turn, not left for the next

    client = use_bots(tool_call("pantry_count", {"item": "eggs"}), say("never sent"))
    during_first_call(client, lambda: tasks.request_stop(session.id))
    messaging.deliver(str(delegated(session).id), registry=webhooks.get_registry())
    assert session.structured_calls.latest("created_at").status == "stopped"
    assert len(client.calls) == 1


@pytest.mark.django_db(transaction=True)
def test_a_stop_meant_for_one_delegated_turn_does_not_hit_the_next(session, use_bots, held_messages):  # noqa: F811
    from django_ergo.bots import messaging, webhooks

    client = use_bots(tool_call("pantry_count", {"item": "eggs"}), say("never sent"))
    during_first_call(client, lambda: tasks.request_stop(session.id))
    messaging.deliver(str(delegated(session).id), registry=webhooks.get_registry())
    assert session.structured_calls.get().status == "stopped"
    assert tasks.stop_requested(session.id)  # left over: the stopped turn never reached its end of loop

    # The next queued message runs to its end under the session lock, as a worker runs it.
    client = use_bots(tool_call("pantry_count", {"item": "eggs"}), say("Four eggs."))
    next_message = delegated(session)
    assert tasks.deliver_locked(str(next_message.id)) == session.id
    assert session.structured_calls.latest("created_at").status == "completed"
    assert len(client.calls) == 2
    assert not tasks.stop_requested(session.id)


@pytest.mark.django_db(transaction=True)
def test_a_thread_message_queued_during_a_turn_is_dispatched_once_the_lock_is_free(
    session,  # noqa: F811
    use_bots,  # noqa: F811
    settings,
):
    use_bots(say("Four eggs."))
    # Each dispatch records whether the session's turn lock was still held.
    dispatched = []
    settings.DJANGO_ERGO = {
        **settings.DJANGO_ERGO,
        "THREAD_MESSAGE_RUNNER": lambda message_id: dispatched.append(tasks.turn_running(session.id)),
    }
    queued = delegated(session)  # waiting for the user's turn below
    tasks.push_message(session.id, "How many eggs?")
    tasks.run_turn(str(session.id))

    queued.refresh_from_db()
    assert queued.status == "queued"
    # The turn's own end dispatches it too early (the lock is held until run_turn returns).
    # Only a dispatch after the release reaches a worker that can take the lock.
    assert dispatched[-1] is False


@pytest.mark.django_db(transaction=True)
def test_a_waiting_message_can_be_unsent_until_the_model_takes_it(client, cook, use_bots):  # noqa: F811
    use_bots(say("hi"))
    root = post(client, "/api/bots/kitchen/root").json()
    first = tasks.push_message(root["id"], "Use blue")
    second = tasks.push_message(root["id"], "Actually red")

    inbox = client.get(f"/api/sessions/{root['id']}").json()["inbox"]
    assert [(i["id"], i["text"]) for i in inbox] == [(first, "Use blue"), (second, "Actually red")]

    taken = client.delete(f"/api/sessions/{root['id']}/inbox/{first}")
    assert taken.json() == {"text": "Use blue", "attachment_ids": []}
    assert [i["text"] for i in tasks.peek_inbox(root["id"])] == ["Actually red"]

    assert [i["text"] for i in tasks.drain_inbox(root["id"])] == ["Actually red"]  # the turn took it
    assert client.delete(f"/api/sessions/{root['id']}/inbox/{second}").status_code == 409
    assert client.get(f"/api/sessions/{root['id']}").json()["inbox"] == []


@pytest.mark.django_db(transaction=True)
def test_usage_is_saved_per_request_and_counted_once_across_an_approval(session, use_bots):  # noqa: F811
    from django_ergo.conversation.models import StructuredCall

    # Each fake response uses 10 input and 5 output tokens.
    client = use_bots(tool_call("pantry_count", {"item": "eggs"}), tool_call("order", {"item": "eggs"}), say("Done."))
    seen = []
    create = client.create

    async def watch(**kwargs):
        if len(client.calls) == 1:  # the second request: the first one's usage is already saved
            from asgiref.sync import sync_to_async

            call = await sync_to_async(StructuredCall.objects.get)(session=session)
            seen.append((call.status, call.input_tokens, call.output_tokens))
        return await create(**kwargs)

    client.create = watch
    tasks.push_message(session.id, "Order eggs")
    tasks.run_turn(str(session.id))
    call = session.structured_calls.get()
    assert seen == [("in_progress", 10, 5)]
    assert (call.status, call.input_tokens, call.output_tokens) == ("awaiting_approval", 20, 10)

    tasks.run_turn(str(session.id), approve=True)
    call.refresh_from_db()
    assert (call.status, call.input_tokens, call.output_tokens) == ("completed", 30, 15)
