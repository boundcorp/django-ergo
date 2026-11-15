"""Bound work to sessions/messages, not the incoming × outgoing request product."""

import json
from types import SimpleNamespace

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django_ergo.conversation.models import (
    ConversationSession,
    StructuredCall,
    ThreadMessage,
)

from ergonaut.api.bots import list_sessions, sent_out, with_open_counts
from ergonaut.apps.bots.tests.test_api import bot_folder, cook, use_bots  # noqa: F401


@pytest.mark.django_db
def test_bidirectional_counts_preserve_semantics_without_join_fanout(cook):  # noqa: F811
    center = ConversationSession.objects.create(user=cook, bot_name="kitchen")
    other = ConversationSession.objects.create(user=cook, bot_name="kitchen")
    incoming = ThreadMessage.objects.bulk_create(
        [
            ThreadMessage(sender_session=other, recipient_session=center, text="request", status="waiting")
            for _ in range(30)
        ]
    )
    ThreadMessage.objects.bulk_create(
        [
            ThreadMessage(sender_session=center, recipient_session=other, text="request", status="delivered")
            for _ in range(40)
        ]
    )
    ThreadMessage.objects.create(sender_session=other, recipient_session=center, text="done", status="answered")
    ThreadMessage.objects.create(sender_session=other, recipient_session=center, text="failed", status="failed")
    ThreadMessage.objects.create(recipient_session=center, text="person", status="queued")
    ThreadMessage.objects.create(
        sender_session=other, recipient_session=center, in_reply_to=incoming[0], text="reply", status="queued"
    )
    ThreadMessage.objects.create(
        sender_session=center, recipient_session=other, in_reply_to=incoming[0], text="reply", status="waiting"
    )
    qs = with_open_counts(ConversationSession.objects.filter(pk=center.pk))
    row = qs.get()
    assert (row.open_in, row.open_out) == (30, 40)
    assert row.busy is False
    assert row.blocked is False
    empty = ConversationSession.objects.create(user=cook, bot_name="kitchen")
    empty = with_open_counts(ConversationSession.objects.filter(pk=empty.pk)).get()
    assert (empty.open_in, empty.open_out) == (0, 0)

    # A query-count assertion alone missed this production regression: one SQL
    # statement materialized thousands of rows. Assert actual executor work,
    # not SQL text, node names, planner costs or machine-dependent wall time.
    plan = json.loads(qs.explain(format="json", analyze=True))[0]["Plan"]

    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    assert max(node.get("Actual Rows", 0) for node in nodes(plan)) <= 80


@pytest.mark.django_db
def test_sidebar_queries_do_not_grow_with_session_count(cook, use_bots):  # noqa: F811
    use_bots()
    request = SimpleNamespace(auth=cook)
    ConversationSession.objects.create(user=cook, bot_name="kitchen", metadata={"bot_role": "thread"})
    with CaptureQueriesContext(connection) as first:
        assert len(list_sessions(request)) == 1
    ConversationSession.objects.bulk_create(
        [ConversationSession(user=cook, bot_name="kitchen", metadata={"bot_role": "thread"}) for _ in range(49)]
    )
    with CaptureQueriesContext(connection) as many:
        rows = list_sessions(request)
    assert len(rows) == 50
    assert {row["username"] for row in rows} == {cook.get_username()}
    assert len(many) == len(first) == 2


@pytest.mark.django_db
def test_repeated_sent_cards_reuse_thread_status_and_keep_individual_requests(cook):  # noqa: F811
    sender = ConversationSession.objects.create(user=cook, bot_name="kitchen")
    target = ConversationSession.objects.create(user=cook, bot_name="kitchen", metadata={"bot_role": "thread"})
    StructuredCall.objects.create(session=target, kind="chat_reply", status="completed", response={"type": "question"})
    ThreadMessage.objects.create(sender_session=sender, recipient_session=target, text="first", status="answered")
    with CaptureQueriesContext(connection) as one:
        initial = sent_out(sender)
    ThreadMessage.objects.bulk_create(
        [
            ThreadMessage(sender_session=sender, recipient_session=target, text=f"request {i}", status="queued")
            for i in range(29)
        ]
    )
    with CaptureQueriesContext(connection) as many:
        cards = sent_out(sender)
    assert len(cards) == 30
    assert len({card["message_id"] for card in cards}) == 30
    assert {card["status"] for card in cards} == {"queued", "done"}
    assert all(card["thread"]["last_asks"] for card in cards)
    assert {card["thread"]["id"] for card in cards} == {str(target.pk)}
    assert initial[0]["thread"]["last_asks"] is True
    assert len(many) == len(one)
