"""Reply updates: a bot revises its replies in place, unless the chat owner turns updates off.

A chat with ``ConversationSession.reply_updates`` on gets one more tool and
two more ``send_reply`` fields:

- ``ergo_interim_reply`` with ``action="append"`` posts a new reply without
  ending the turn. With ``action="update"`` it revises one of the bot's
  replies (``reply_id``, ``revision`` it last saw). It never ends the turn.
- ``send_reply`` with ``update_reply_id`` and ``update_revision`` ends the
  turn by revising that reply instead of adding a new one.

A reply's id is the tool_use id of the call that made it (a ``send_reply``
or an appending ``ergo_interim_reply``); its revision is 0 until changed.
Each accepted change is a ``ReplyRevision`` row and the tool call stays in
the transcript, so every version is kept. The server checks every change:

- the target is a reply in this chat, written by this bot, not a question
  and not itself an update;
- no human message came after it (people's messages are boundaries);
- ``revision`` is the current one (stale or concurrent updates fail, and the
  error carries the current revision and text);
- a replayed tool call (same tool_use id) returns its first receipt.

Separate replies are always allowed; the instructions only prefer updates.
When turned off, none of this is offered and every reply is a new message.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from typing import Literal

from django.db import IntegrityError
from django.db import transaction
from pydantic import BaseModel
from pydantic import Field
from pydantic import ValidationError

from django_ergo.conversation.tool_dispatch import ToolError
from django_ergo.conversation.tool_dispatch import current_tool_use_id
from django_ergo.conversation.toolkit import Toolkit

if TYPE_CHECKING:
    from django_ergo.conversation.adapters import ToolAdapter
    from django_ergo.conversation.models import ConversationSession

INTERIM_REPLY_TOOL = "ergo_interim_reply"
REPLY_TOOL = "send_reply"  # chat_reply.CHAT_REPLY_TOOL (imported there from here)
MAX_ATTACHMENTS = 50
CONTEXT_REPLIES = 5

REPLY_UPDATES_INSTRUCTIONS = f"""\
Reply updates are on in this chat: a reply you update changes in place, so \
the chat stays still while you work.
- To show progress without ending your turn, call {INTERIM_REPLY_TOOL}: \
action "append" posts a new reply; action "update" revises one of your \
replies since the person's last message (give its reply_id and the revision \
you last saw). Each call returns the reply_id and its new revision.
- For progress on the same piece of work, prefer updating your latest reply \
to adding another. To end the turn by revising it, call {REPLY_TOOL} with \
update_reply_id and update_revision.
- Send a separate reply when it is a separate message the person should see \
on its own. Questions are always new replies.
- If an update is refused as stale, the error gives the current revision and \
text: read it, then update again with that revision or append instead."""


class InterimReply(BaseModel):
    action: Literal["append", "update"] = Field(
        description='"append" posts a new reply; "update" revises reply_id'
    )
    text: str = Field(
        min_length=1, description="What the user sees, in plain conversational text"
    )
    reply_id: str = Field(
        default="", description="update: the reply to revise (from a receipt)"
    )
    revision: int | None = Field(
        default=None,
        ge=0,
        description="update: the reply's current revision, as you last saw it",
    )
    attachments: list[str] = Field(
        default_factory=list,
        max_length=MAX_ATTACHMENTS,
        description="Files to show with this reply, by id or filename",
    )


def enabled(session: ConversationSession | None) -> bool:
    """Whether the chat has reply updates on, read fresh (the user may switch it)."""
    from django_ergo.conversation.models import ConversationSession

    if session is None or session.pk is None:
        return False
    return bool(
        ConversationSession.objects.filter(pk=session.pk)
        .values_list("reply_updates", flat=True)
        .first()
    )


def _content(arguments: dict) -> dict:
    return {
        "type": arguments.get("type") or "message",
        "text": arguments.get("text") or "",
        "attachments": list(arguments.get("attachments") or []),
        "suggestions": list(arguments.get("suggestions") or []),
    }


def _human(message) -> bool:
    """A person's message: a user-role message that is not tool results or Ergo's/an event's."""
    if message.role != "user":
        return False
    if (message.author or {}).get("kind") == "system":
        return False
    return any(b.block_type != "tool_result" for b in message.content_blocks.all())


def _receipt(reply_id: str, revision: int, action: str, **extra) -> str:
    return json.dumps(
        {"reply_id": reply_id, "revision": revision, "action": action, **extra}
    )


def _target(session, reply_id: str):
    """The reply block ``reply_id`` names, checked for being revisable."""
    from django_ergo.conversation.models import MessageBlock

    block = (
        MessageBlock.objects.filter(
            message__session=session,
            block_type="tool_use",
            tool_use_id=reply_id,
            tool_name__in=[REPLY_TOOL, INTERIM_REPLY_TOOL],
        )
        .select_related("message")
        .first()
    )
    if block is None:
        msg = "No reply with this id in this chat."
        raise ToolError(msg, code="not_found", reply_id=reply_id)
    arguments = block.tool_input or {}
    message = block.message
    author = message.author or {}
    if message.role != "assistant" or (
        author
        and (author.get("kind") != "bot" or author.get("ref") != session.bot_name)
    ):
        msg = "That reply was not written by you."
        raise ToolError(
            msg,
            code="foreign_author",
            reply_id=reply_id,
        )
    if block.tool_name == INTERIM_REPLY_TOOL and arguments.get("action") != "append":
        original = arguments.get("reply_id") or ""
        msg = "That id is an update; revise the original reply."
        raise ToolError(
            msg,
            code="is_update",
            reply_id=original,
        )
    if block.tool_name == REPLY_TOOL and arguments.get("update_reply_id"):
        msg = "That id is an update; revise the original reply."
        raise ToolError(
            msg,
            code="is_update",
            reply_id=arguments["update_reply_id"],
        )
    if (arguments.get("type") or "message") == "question":
        msg = "Questions are not revised; send a new reply instead."
        raise ToolError(
            msg,
            code="question",
            reply_id=reply_id,
        )
    result = MessageBlock.objects.filter(
        message__session=session, block_type="tool_result", tool_result_for=reply_id
    ).first()
    # A final reply is delivered once its result is stored; an interim one may
    # still be in the current batch, but a failed one is no reply.
    if (result is not None and result.is_error) or (
        result is None and block.tool_name == REPLY_TOOL
    ):
        msg = "That reply was not delivered."
        raise ToolError(msg, code="not_delivered", reply_id=reply_id)
    later = (
        session.messages.filter(sequence__gt=message.sequence, role="user")
        .prefetch_related("content_blocks")
        .order_by("sequence")
    )
    if any(_human(m) for m in later):
        msg = "The person has written since that reply; send a new reply instead."
        raise ToolError(
            msg,
            code="human_boundary",
            reply_id=reply_id,
        )
    return block


def current(session, reply_id: str, original: dict) -> tuple[int, dict]:
    """The reply's current (revision, content)."""
    latest = (
        session.reply_revisions.filter(reply_id=reply_id).order_by("-revision").first()
    )
    if latest is None:
        return 0, _content(original)
    return latest.revision, latest.content


def revise(  # noqa: PLR0913
    session: ConversationSession,
    *,
    tool_use_id: str,
    tool_name: str,
    reply_id: str,
    revision: int | None,
    content: dict,
) -> str:
    """Record one revision of ``reply_id`` and return its receipt (a JSON string).

    Raises ``ToolError`` (``code`` plus details) when the change isn't allowed.
    """
    from django_ergo.conversation.models import ConversationSession
    from django_ergo.conversation.models import ReplyRevision

    if not tool_use_id:
        msg = "Reply updates work only inside a chat turn."
        raise ToolError(msg, code="no_turn")
    if not reply_id or revision is None:
        msg = "An update needs reply_id and the revision you last saw."
        raise ToolError(
            msg,
            code="missing_target",
        )
    with transaction.atomic():
        # The lock new messages take (conversation.messages.add_message), so no
        # human message lands between the boundary check and the write.
        locked = (
            ConversationSession.objects.select_for_update()
            .only("pk", "reply_updates", "bot_name")
            .get(pk=session.pk)
        )
        prior = ReplyRevision.objects.filter(
            session=locked, tool_use_id=tool_use_id
        ).first()
        if prior is not None:
            if prior.reply_id != reply_id:
                msg = "This tool call already revised another reply."
                raise ToolError(
                    msg,
                    code="replay_mismatch",
                    reply_id=prior.reply_id,
                )
            # Before the switch check: a resumed turn replays calls already made.
            return _receipt(reply_id, prior.revision, "update", replayed=True)
        if not locked.reply_updates:
            msg = "Reply updates are off in this chat; send a new reply."
            raise ToolError(msg, code="disabled")
        target = _target(locked, reply_id)
        now, shown = current(locked, reply_id, target.tool_input or {})
        if revision != now:
            if revision == now - 1 and shown == content:
                # A retry of an update that already went in under another call.
                return _receipt(reply_id, now, "update", already_applied=True)
            msg = f"Stale revision {revision}; the reply is at revision {now}."
            raise ToolError(
                msg,
                code="stale",
                reply_id=reply_id,
                current_revision=now,
                current_text=shown.get("text", ""),
            )
        try:
            with transaction.atomic():
                ReplyRevision.objects.create(
                    session=locked,
                    reply_id=reply_id,
                    revision=now + 1,
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                    content=content,
                )
        except IntegrityError as exc:
            msg = "Another update went in first; read the reply and try again."
            raise ToolError(
                msg,
                code="stale",
                reply_id=reply_id,
            ) from exc
    return _receipt(reply_id, now + 1, "update")


class InterimReplyToolkit(Toolkit):
    """``ergo_interim_reply`` for one chat: append or revise without ending the turn."""

    def __init__(self, session: ConversationSession):
        self.session = session

    def has_tool(self, tool_name: str) -> bool:
        return tool_name == INTERIM_REPLY_TOOL

    def get_tools_schema(self, adapter: ToolAdapter) -> list[dict]:
        from django_ergo.conversation.adapters import OpenAIToolAdapter

        description = (
            "Post a new reply (append) or revise one of your replies since the "
            "person's last message (update), without ending your turn. Returns "
            "the reply_id and its revision. Finish the turn with send_reply."
        )
        schema = InterimReply.model_json_schema()
        if isinstance(adapter, OpenAIToolAdapter):
            return [
                {
                    "type": "function",
                    "function": {
                        "name": INTERIM_REPLY_TOOL,
                        "description": description,
                        "parameters": schema,
                    },
                }
            ]
        return [
            {
                "name": INTERIM_REPLY_TOOL,
                "description": description,
                "input_schema": schema,
            }
        ]

    def execute_tool(self, tool_name: str, arguments: dict) -> str:
        try:
            reply = InterimReply.model_validate(arguments)
        except ValidationError as e:
            msg = f"Invalid arguments: {e}"
            raise ToolError(msg, code="invalid") from e
        tool_use_id = current_tool_use_id.get() or ""
        if reply.action == "append":
            if not enabled(self.session):
                msg = "Reply updates are off in this chat; use send_reply."
                raise ToolError(
                    msg,
                    code="disabled",
                )
            if not tool_use_id:
                msg = "Reply updates work only inside a chat turn."
                raise ToolError(msg, code="no_turn")
            # The reply is this tool call itself; a replay gives the same receipt.
            return _receipt(tool_use_id, 0, "append")
        return revise(
            self.session,
            tool_use_id=tool_use_id,
            tool_name=INTERIM_REPLY_TOOL,
            reply_id=reply.reply_id,
            revision=reply.revision,
            content=_content(reply.model_dump()),
        )

    def render_overview(self) -> str:
        return ""


def final_reply_check(session: ConversationSession):
    """The ``send_reply`` check for a chat with reply updates: an update field revises."""

    def check(parsed: BaseModel) -> None:
        reply_id = getattr(parsed, "update_reply_id", "") or ""
        if not reply_id:
            return
        if getattr(parsed, "type", "message") == "question":
            msg = "Questions are always new replies; leave update_reply_id empty."
            raise ValueError(msg)
        try:
            revise(
                session,
                tool_use_id=current_tool_use_id.get() or "",
                tool_name=REPLY_TOOL,
                reply_id=reply_id,
                revision=getattr(parsed, "update_revision", None),
                content=_content(parsed.model_dump()),
            )
        except ToolError as e:
            raise ValueError(json.dumps({"error": str(e), **e.fields})) from e

    return check


def context_text(session: ConversationSession) -> str:
    """The bot's revisable replies since the person's last message, newest last."""
    from django_ergo.conversation.models import MessageBlock

    blocks = list(
        MessageBlock.objects.filter(
            message__session=session,
            message__role="assistant",
            block_type="tool_use",
            tool_name__in=[REPLY_TOOL, INTERIM_REPLY_TOOL],
        )
        .select_related("message")
        .order_by("-message__sequence", "-sequence")[:50]
    )
    lines = []
    for block in blocks:
        if len(lines) >= CONTEXT_REPLIES:
            break
        try:
            _target(session, block.tool_use_id)
        except ToolError as e:
            if e.fields.get("code") == "human_boundary":
                break
            continue
        revision, content = current(session, block.tool_use_id, block.tool_input or {})
        text = " ".join((content.get("text") or "").split())
        if len(text) > 120:  # noqa: PLR2004
            text = text[:117] + "..."
        lines.append(f'- reply_id {block.tool_use_id}, revision {revision}: "{text}"')
    if not lines:
        return "No replies since the person's last message."
    return "\n".join(reversed(lines))
