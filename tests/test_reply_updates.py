"""Reply updates: the per-chat preference, ergo_interim_reply and send_reply updates.

These tests establish the server contract (scope, preconditions, replay,
history). They say nothing about how well a model uses the tools.
"""

from __future__ import annotations

import json
import threading

import pytest
from django.contrib.auth import get_user_model
from django.db import connection

from django_ergo.conversation import reply_updates
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import MessageBlock
from django_ergo.conversation.models import ReplyRevision
from django_ergo.conversation.models import SessionMessage
from django_ergo.conversation.tool_dispatch import ToolError
from django_ergo.conversation.tool_dispatch import current_tool_use_id
from tests.test_bots import make_bot
from tests.test_conversation_structured import claude_tool

User = get_user_model()


def _results(engine):
    """Tool results the model got, by tool_use id."""
    out = {}
    for call in engine._client.calls:
        for message in call["messages"]:
            if message["role"] != "user" or isinstance(message["content"], str):
                continue
            for block in message["content"]:
                if block.get("type") == "tool_result":
                    content = block["content"]
                    if isinstance(content, list):
                        content = "".join(p.get("text", "") for p in content)
                    out[block["tool_use_id"]] = (content, block.get("is_error", False))
    return out


def _tools(call):
    return {t["name"]: t for t in call["tools"]}


# -- end to end through a bot turn -------------------------------------------


@pytest.mark.django_db(transaction=True)
async def test_explicit_off_offers_nothing_new(tmp_path):
    user = await User.objects.acreate(username="plain")
    bot, engine = make_bot(
        tmp_path, claude_tool("send_reply", {"text": "Hi"}, tool_id="r0")
    )
    root = await bot.root_session(user)
    assert root.reply_updates is True
    await ConversationSession.objects.filter(pk=root.pk).aupdate(reply_updates=False)
    await root.arefresh_from_db()
    await bot.ask(root, "Hello")
    tools = _tools(engine._client.calls[0])
    assert reply_updates.INTERIM_REPLY_TOOL not in tools
    assert "update_reply_id" not in json.dumps(tools["send_reply"])
    assert "Reply updates are on" not in json.dumps(engine._client.calls[0])


@pytest.mark.django_db(transaction=True)
async def test_append_update_then_final_update_keeps_every_version(tmp_path):
    user = await User.objects.acreate(username="worker")
    tool = reply_updates.INTERIM_REPLY_TOOL
    bot, engine = make_bot(
        tmp_path,
        claude_tool(tool, {"action": "append", "text": "Starting"}, tool_id="i1"),
        claude_tool("pantry_count", {"item": "eggs"}, tool_id="t1"),
        claude_tool(
            tool,
            {
                "action": "update",
                "reply_id": "i1",
                "revision": 0,
                "text": "Half done",
                "attachments": ["chart.png"],
            },
            tool_id="u1",
        ),
        # A deliberate separate reply is still allowed.
        claude_tool(tool, {"action": "append", "text": "Side note"}, tool_id="i2"),
        claude_tool("pantry_count", {"item": "beans"}, tool_id="t2"),
        claude_tool(
            "send_reply",
            {"text": "All done", "update_reply_id": "i1", "update_revision": 1},
            tool_id="f1",
        ),
    )
    root = await bot.root_session(user)
    assert root.reply_updates is True

    result = await bot.ask(root, "Do the work")

    first = engine._client.calls[0]
    assert tool in _tools(first)
    assert "update_reply_id" in json.dumps(_tools(first)["send_reply"])
    assert "Reply updates are on" in json.dumps(first)
    assert "Your replies you can update" in json.dumps(first)
    # The final reply is still the turn's terminal answer.
    assert result.text == "All done"
    assert result.reply.update_reply_id == "i1"
    assert len(engine._client.calls) == 6

    results = _results(engine)
    assert json.loads(results["t1"][0]) == {"item": "eggs", "count": 4}
    assert json.loads(results["t2"][0]) == {"item": "beans", "count": 0}
    assert json.loads(results["i1"][0]) == {
        "reply_id": "i1",
        "revision": 0,
        "action": "append",
    }
    assert json.loads(results["u1"][0]) == {
        "reply_id": "i1",
        "revision": 1,
        "action": "update",
    }
    assert json.loads(results["i2"][0])["reply_id"] == "i2"

    rows = [r async for r in ReplyRevision.objects.filter(session=root)]
    assert [(r.reply_id, r.revision, r.tool_use_id) for r in rows] == [
        ("i1", 1, "u1"),
        ("i1", 2, "f1"),
    ]
    assert rows[0].content["attachments"] == ["chart.png"]
    # Exact history: every call stays in the transcript, none is rewritten.
    stored = [
        (b.tool_use_id, b.tool_input.get("text"))
        async for b in MessageBlock.objects.filter(
            message__session=root, block_type="tool_use"
        ).order_by("message__sequence", "sequence")
    ]
    assert stored == [
        ("i1", "Starting"),
        ("t1", None),
        ("u1", "Half done"),
        ("i2", "Side note"),
        ("t2", None),
        ("f1", "All done"),
    ]


@pytest.mark.django_db(transaction=True)
async def test_stale_final_update_is_returned_to_the_model_with_recovery(tmp_path):
    user = await User.objects.acreate(username="stale")
    tool = reply_updates.INTERIM_REPLY_TOOL
    bot, engine = make_bot(
        tmp_path,
        claude_tool(tool, {"action": "append", "text": "Working"}, tool_id="i1"),
        claude_tool(
            "send_reply",
            {"text": "Done", "update_reply_id": "i1", "update_revision": 3},
            tool_id="f1",
        ),
        claude_tool(
            "send_reply",
            {"text": "Done", "update_reply_id": "i1", "update_revision": 0},
            tool_id="f2",
        ),
    )
    root = await bot.root_session(user)
    await ConversationSession.objects.filter(pk=root.pk).aupdate(reply_updates=True)
    await root.arefresh_from_db()
    result = await bot.ask(root, "Go")
    content, is_error = _results(engine)["f1"]
    assert is_error
    assert '"code": "stale"' in content
    assert '"current_revision": 0' in content
    assert '"current_text": "Working"' in content
    assert result.text == "Done"
    assert await ReplyRevision.objects.filter(session=root).acount() == 1


# -- the server rules ---------------------------------------------------------


def _chat(user, bot="kitchen", *, on=True):
    return ConversationSession.objects.create(
        user=user,
        engine_type="claude",
        transport_type="api",
        status="active",
        bot_name=bot,
        reply_updates=on,
    )


def _msg(session, role, blocks, **fields):
    last = session.messages.order_by("-sequence").first()
    message = SessionMessage.objects.create(
        session=session,
        role=role,
        sequence=0 if last is None else last.sequence + 1,
        **fields,
    )
    for i, block in enumerate(blocks):
        MessageBlock.objects.create(message=message, sequence=i, **block)
    return message


def _reply(session, tool_id, text, *, kind="message", tool="send_reply", **fields):
    arguments = {"type": kind, "text": text}
    if tool == reply_updates.INTERIM_REPLY_TOOL:
        arguments = {"action": "append", "text": text}
    _msg(
        session,
        "assistant",
        [
            {
                "block_type": "tool_use",
                "tool_use_id": tool_id,
                "tool_name": tool,
                "tool_input": arguments,
            }
        ],
        **fields,
    )
    _msg(
        session,
        "user",
        [
            {
                "block_type": "tool_result",
                "tool_result_for": tool_id,
                "tool_result_content": "ok",
            }
        ],
    )


def _human(session, text="Thanks"):
    _msg(session, "user", [{"block_type": "text", "text": text}])


def _revise(session, tool_use_id, reply_id, revision, text="New"):
    return json.loads(
        reply_updates.revise(
            session,
            tool_use_id=tool_use_id,
            tool_name=reply_updates.INTERIM_REPLY_TOOL,
            reply_id=reply_id,
            revision=revision,
            content={
                "type": "message",
                "text": text,
                "attachments": [],
                "suggestions": [],
            },
        )
    )


def _code(fn):
    with pytest.raises(ToolError) as exc:
        fn()
    return exc.value.fields.get("code")


@pytest.mark.django_db
def test_stale_retry_and_replay():
    user = User.objects.create(username="rules")
    chat = _chat(user)
    _reply(chat, "r1", "Running tests")
    assert _revise(chat, "u1", "r1", 0, "Tests passed")["revision"] == 1
    # The same tool call again (a resumed or retried turn) returns its first receipt.
    assert _revise(chat, "u1", "r1", 0, "Tests passed") == {
        "reply_id": "r1",
        "revision": 1,
        "action": "update",
        "replayed": True,
    }
    # A new call repeating the applied update is recognised, not applied twice.
    assert _revise(chat, "u2", "r1", 0, "Tests passed")["already_applied"] is True
    # A different change from a stale revision is refused with the current state.
    with pytest.raises(ToolError) as exc:
        _revise(chat, "u3", "r1", 0, "Something else")
    assert exc.value.fields["code"] == "stale"
    assert exc.value.fields["current_revision"] == 1
    assert exc.value.fields["current_text"] == "Tests passed"
    # A replayed call id cannot be redirected to another reply.
    _reply(chat, "r2", "Other")
    assert _code(lambda: _revise(chat, "u1", "r2", 0)) == "replay_mismatch"
    assert ReplyRevision.objects.filter(session=chat).count() == 1


@pytest.mark.django_db
def test_human_boundary_but_not_events():
    user = User.objects.create(username="bounds")
    chat = _chat(user)
    _reply(chat, "r1", "Started the deploy")
    # An event (a worker finishing) is not a person: the reply can still change.
    _msg(
        chat,
        "user",
        [{"block_type": "text", "text": "Worker finished"}],
        author={"kind": "system", "ref": "worker:1", "display_name": "Worker"},
    )
    assert _revise(chat, "u1", "r1", 0, "Deploy live")["revision"] == 1
    assert "r1" in reply_updates.context_text(chat)
    _human(chat)
    assert _code(lambda: _revise(chat, "u2", "r1", 1)) == "human_boundary"
    assert reply_updates.context_text(chat) == (
        "No replies since the person's last message."
    )


@pytest.mark.django_db
def test_scope_author_questions_and_toggle():
    user = User.objects.create(username="scope")
    chat, other = _chat(user), _chat(user)
    _reply(other, "o1", "In another chat")
    assert _code(lambda: _revise(chat, "u1", "o1", 0)) == "not_found"
    _reply(
        chat,
        "f1",
        "From another bot",
        author={"kind": "bot", "ref": "someone-else", "display_name": "x"},
    )
    assert _code(lambda: _revise(chat, "u2", "f1", 0)) == "foreign_author"
    _reply(chat, "q1", "Tacos or soup?", kind="question")
    assert _code(lambda: _revise(chat, "u3", "q1", 0)) == "question"
    _reply(chat, "r1", "Mine")
    _revise(chat, "u4", "r1", 0)
    # An update is not itself a reply to revise.
    _msg(
        chat,
        "assistant",
        [
            {
                "block_type": "tool_use",
                "tool_use_id": "u5",
                "tool_name": reply_updates.INTERIM_REPLY_TOOL,
                "tool_input": {"action": "update", "reply_id": "r1", "text": "x"},
            }
        ],
    )
    with pytest.raises(ToolError) as exc:
        _revise(chat, "u6", "u5", 0)
    assert exc.value.fields == {"code": "is_update", "reply_id": "r1"}
    ConversationSession.objects.filter(pk=chat.pk).update(reply_updates=False)
    assert _code(lambda: _revise(chat, "u7", "r1", 1)) == "disabled"
    # A resumed turn replaying an accepted call still gets its receipt.
    assert _revise(chat, "u4", "r1", 0)["replayed"] is True


@pytest.mark.django_db
def test_failed_reply_is_not_a_target():
    user = User.objects.create(username="failed")
    chat = _chat(user)
    _msg(
        chat,
        "assistant",
        [
            {
                "block_type": "tool_use",
                "tool_use_id": "bad",
                "tool_name": "send_reply",
                "tool_input": {"text": ""},
            }
        ],
    )
    _msg(
        chat,
        "user",
        [
            {
                "block_type": "tool_result",
                "tool_result_for": "bad",
                "tool_result_content": "Output failed validation",
                "is_error": True,
            }
        ],
    )
    assert _code(lambda: _revise(chat, "u1", "bad", 0)) == "not_delivered"


@pytest.mark.django_db
def test_interim_toolkit_needs_a_turn_and_validates():
    user = User.objects.create(username="kit")
    chat = _chat(user)
    kit = reply_updates.InterimReplyToolkit(chat)
    assert _code(lambda: kit.execute_tool("x", {"action": "append", "text": "a"})) == (
        "no_turn"
    )
    assert _code(lambda: kit.execute_tool("x", {"action": "nope", "text": "a"})) == (
        "invalid"
    )
    token = current_tool_use_id.set("i9")
    try:
        assert json.loads(kit.execute_tool("x", {"action": "append", "text": "a"})) == {
            "reply_id": "i9",
            "revision": 0,
            "action": "append",
        }
        assert _code(
            lambda: kit.execute_tool("x", {"action": "update", "text": "b"})
        ) == ("missing_target")
    finally:
        current_tool_use_id.reset(token)


@pytest.mark.django_db
def test_final_question_cannot_update():
    from django_ergo.conversation.chat_reply import UpdatingChatReply

    user = User.objects.create(username="q")
    chat = _chat(user)
    check = reply_updates.final_reply_check(chat)
    check(UpdatingChatReply(text="plain"))  # no update: nothing to check
    with pytest.raises(ValueError, match="Questions are always new replies"):
        check(
            UpdatingChatReply(
                type="question", text="?", update_reply_id="r1", update_revision=0
            )
        )


@pytest.mark.django_db
def test_toggle_survives_a_stale_full_save_and_is_per_chat():
    user = User.objects.create(username="toggle")
    chat, other = _chat(user, on=False), _chat(user, on=False)
    stale = ConversationSession.objects.get(pk=chat.pk)
    ConversationSession.objects.filter(pk=chat.pk).update(reply_updates=True)
    stale.metadata = {"note": "x"}
    stale.save()
    chat.refresh_from_db()
    other.refresh_from_db()
    assert chat.reply_updates is True
    assert other.reply_updates is False


@pytest.mark.django_db(transaction=True)
def test_concurrent_updates_one_wins():
    user = User.objects.create(username="race")
    chat = _chat(user)
    _reply(chat, "r1", "Start")
    barrier = threading.Barrier(2)
    outcomes = []

    def update(tool_id):
        barrier.wait()
        try:
            outcomes.append(
                _revise(chat, tool_id, "r1", 0, f"from {tool_id}")["revision"]
            )
        except ToolError as e:
            outcomes.append(e.fields["code"])
        finally:
            connection.close()

    threads = [threading.Thread(target=update, args=(t,)) for t in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(map(str, outcomes)) == ["1", "stale"]
    assert ReplyRevision.objects.filter(session=chat).count() == 1
