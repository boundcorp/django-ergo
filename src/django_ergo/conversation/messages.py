"""Storing a session's messages, the same way for every engine.

Messages are ``SessionMessage`` rows of ``MessageBlock``s (see the models).
Engines write through these helpers and render the rows into their own API's
format when they send them, so the model, and the engine it runs on, can
change between turns.
"""

from __future__ import annotations

from typing import Any

from asgiref.sync import sync_to_async
from django.db.models import Max

from django_ergo.conversation.identity import bot_identity
from django_ergo.conversation.identity import django_user_identity
from django_ergo.conversation.images import result_content
from django_ergo.conversation.images import stored_result

# What the output tool of a structured call returns when it takes the answer.
OUTPUT_ACCEPTED = "Output accepted."


def is_response_copy(message, previous) -> bool:
    """True for the plain-text copy of an accepted answer that closes a turn.

    The copy is stored so chat views and history tools see the answer, but it
    isn't sent back to the model: a history where every turn ends with the
    answer in plain text teaches the model to answer in plain text instead of
    calling the output tool.
    """
    if previous is None or message.role != "assistant" or previous.role != "user":
        return False
    blocks = list(message.content_blocks.all())
    if not blocks or any(b.block_type != "text" for b in blocks):
        return False
    return any(
        b.block_type == "tool_result"
        and not b.is_error
        and b.tool_result_content == OUTPUT_ACCEPTED
        for b in previous.content_blocks.all()
    )


def model_rows(rows) -> list:
    """The stored messages that go to the model: all but answer copies."""
    kept, previous = [], None
    for row in rows:
        if not is_response_copy(row, previous):
            kept.append(row)
        previous = row
    return kept


def next_sequence(session) -> int:
    """The sequence number the session's next message gets."""
    last = session.messages.aggregate(last=Max("sequence"))["last"]
    return 0 if last is None else last + 1


async def anext_sequence(session) -> int:
    last = (await session.messages.aaggregate(last=Max("sequence")))["last"]
    return 0 if last is None else last + 1


async def tool_result_content(session, result: Any) -> str | list[dict]:
    """What a tool_result block stores: the text, or text plus image references."""
    text, refs = await stored_result(session, result)
    return result_content(text, refs) if refs else text


async def add_message(session, role: str, blocks: list[dict], **fields):
    """Store a message of ``blocks`` (MessageBlock fields) as the session's next one."""
    from django_ergo.conversation.models import MessageBlock
    from django_ergo.conversation.models import SessionMessage

    if (
        role == "assistant"
        and session.bot_name
        and any(block["block_type"] == "text" for block in blocks)
    ):
        fields.setdefault("author", bot_identity(session))
    message = await SessionMessage.objects.acreate(
        session=session, role=role, sequence=await anext_sequence(session), **fields
    )
    # One by one, not bulk_create: post_save on blocks wakes live views.
    for i, block in enumerate(blocks):
        await MessageBlock.objects.acreate(message=message, sequence=i, **block)
    return message


async def add_user_text(session, text: str, *, author=None, provenance=None):
    if author is None:
        author = await sync_to_async(
            lambda: django_user_identity(session.user), thread_sensitive=True
        )()
    return await add_message(
        session,
        "user",
        [{"block_type": "text", "text": text}],
        author=author,
        provenance=provenance or {},
    )


async def add_tool_results(session, results: list[tuple[str, Any, bool]]):
    """Store (tool_use_id, result, is_error) results as one user message."""
    return await add_message(
        session,
        "user",
        [
            {
                "block_type": "tool_result",
                "tool_result_for": tool_use_id,
                "tool_result_content": await tool_result_content(session, result),
                "is_error": is_error,
            }
            for tool_use_id, result, is_error in results
        ],
    )


def tool_use_block(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "block_type": "tool_use",
        "tool_use_id": call_id,
        "tool_name": name,
        "tool_input": arguments,
    }


async def text_to_tool_use(session, call_id: str, name: str, arguments: dict):
    """Turn the session's last assistant message from text into a call to ``name``.

    For a plain-text answer taken as the output tool's arguments: stored as
    the tool call, the model's history never shows an answer given in plain
    text. Thinking blocks stay.
    """
    from django_ergo.conversation.models import MessageBlock

    message = (
        await session.messages.filter(role="assistant").order_by("-sequence").afirst()
    )
    await message.content_blocks.filter(block_type="text").adelete()
    last = await message.content_blocks.order_by("-sequence").afirst()
    await MessageBlock.objects.acreate(
        message=message,
        sequence=last.sequence + 1 if last else 0,
        **tool_use_block(call_id, name, arguments),
    )
    message.stop_reason = "tool_use"
    await message.asave(update_fields=["stop_reason"])


async def add_tool_exchange(session, calls) -> None:
    """Store tool calls the model didn't make itself (SeededToolCall) and their results."""
    if not calls:
        return
    await add_message(
        session,
        "assistant",
        [tool_use_block(c.tool_use_id, c.name, c.input) for c in calls],
        stop_reason="tool_use",
    )
    await add_tool_results(
        session, [(c.tool_use_id, c.result, c.is_error) for c in calls]
    )


class StoredMessagesMixin:
    """Session methods for an engine that keeps history in SessionMessage rows.

    The engine implements ``_call(session, additional_tools)``: render the
    history, call the model, store the reply with ``add_message`` and yield
    its events.
    """

    model: str

    def _call(self, session, additional_tools=None):
        raise NotImplementedError

    async def append_user_message(
        self, session, message: str, attachments=None, *, author=None, provenance=None
    ):
        from django_ergo.conversation.attachments import save_attachments

        row = await add_user_text(
            session, message, author=author, provenance=provenance
        )
        if attachments:
            await save_attachments(session, row.sequence, attachments)

    async def append_tool_exchange(self, session, calls) -> None:
        await add_tool_exchange(session, calls)

    async def append_tool_results(self, session, results) -> None:
        if results:
            await add_tool_results(session, results)

    async def _persist_tool_result(
        self, session, tool_use_id: str, result: Any, is_error: bool = False
    ) -> None:
        await add_tool_results(session, [(tool_use_id, result, is_error)])

    async def append_assistant_text(self, session, text: str) -> None:
        await add_message(
            session,
            "assistant",
            [{"block_type": "text", "text": text}],
            stop_reason="end_turn",
            model_name=self.model,
        )

    async def respond(self, session, additional_tools=None):
        async for event in self._call(session, additional_tools):
            yield event

    async def send(
        self, session, message: str, additional_tools=None, attachments=None
    ):
        await self.append_user_message(session, message, attachments)
        async for event in self._call(session, additional_tools):
            yield event

    async def submit_tool_result(
        self,
        session,
        tool_use_id: str,
        result: Any,
        is_error: bool = False,
        additional_tools=None,
    ):
        await self._persist_tool_result(session, tool_use_id, result, is_error)
        async for event in self._call(session, additional_tools):
            yield event

    async def submit_tool_results_batch(self, session, results, additional_tools=None):
        await self.append_tool_results(session, results)
        async for event in self._call(session, additional_tools):
            yield event
