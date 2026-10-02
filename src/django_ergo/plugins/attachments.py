"""Attachments plugin: files in chat sessions that the bot can read and write.

    plugins:
      - name: attachments
        max_bytes: 5000000         # largest file the bot may write
        other_sessions: true       # may read files in the user's other sessions

Files come from three places: sent with a message, uploaded to the session
(Ergonaut's Files panel), or written by the bot. Tools:

- ``ergo_attachments_list``: the files in this session, or in another of
  the user's sessions.
- ``ergo_attachments_read``: a file's text (or a description of an image,
  audio clip or binary file).
- ``ergo_attachments_create``: write a new text file into this session.
- ``ergo_attachments_update``: replace the contents of a text file in this
  session.

The bot only writes to its own session. It reads other sessions only when
they belong to the same user. Every turn's context lists the session's
files, so the model knows when something was uploaded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django_ergo.bots.plugins import BotPlugin
from django_ergo.bots.tools import BotTool
from django_ergo.bots.tools import FunctionToolkit
from django_ergo.bots.tools import bot_tool
from django_ergo.conversation.attachments import is_text
from django_ergo.conversation.attachments import read_text
from django_ergo.conversation.attachments import replace_session_file
from django_ergo.conversation.attachments import save_session_file
from django_ergo.conversation.context import TextContextSource

if TYPE_CHECKING:
    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.context import ContextSource
    from django_ergo.conversation.models import ConversationAttachment
    from django_ergo.conversation.models import ConversationSession
    from django_ergo.conversation.toolkit import Toolkit

MAX_LISTED = 50


def describe_row(row: ConversationAttachment) -> dict:
    return {
        "id": str(row.id),
        "filename": row.filename,
        "media_type": row.media_type,
        "size": row.size,
        "source": row.source,
        "updated_at": row.updated_at.isoformat(timespec="seconds"),
    }


class AttachmentsPlugin(BotPlugin):
    name = "attachments"

    def on_load(self) -> None:
        self.max_bytes = int(self.config.get("max_bytes", 5_000_000))
        self.other_sessions = bool(self.config.get("other_sessions", True))

    # -- access --------------------------------------------------------------

    def session_for(self, ctx: ToolContext, session_id: str = "") -> ConversationSession:
        from django_ergo.conversation.models import ConversationSession

        if not session_id or session_id == str(ctx.session.id):
            return ctx.session
        if not self.other_sessions:
            msg = "This bot can only read files in its own session."
            raise ValueError(msg)
        found = ConversationSession.objects.filter(
            id=session_id, user_id=ctx.session.user_id
        ).first()
        if found is None:
            msg = f"No session {session_id} of yours"
            raise ValueError(msg)
        return found

    def file_for(self, ctx: ToolContext, attachment_id: str) -> ConversationAttachment:
        from django_ergo.conversation.models import ConversationAttachment

        row = (
            ConversationAttachment.objects.select_related("session")
            .filter(id=attachment_id, session__user_id=ctx.session.user_id)
            .first()
        )
        if row is None:
            msg = f"No file {attachment_id}"
            raise ValueError(msg)
        if row.session_id != ctx.session.id and not self.other_sessions:
            msg = "This bot can only read files in its own session."
            raise ValueError(msg)
        return row

    # -- tools ---------------------------------------------------------------

    def list_files(self, ctx: ToolContext, session_id: str = "") -> list[dict]:
        session = self.session_for(ctx, session_id)
        rows = session.attachments.order_by("-updated_at")[:MAX_LISTED]
        return [describe_row(r) for r in rows]

    def read(self, ctx: ToolContext, attachment_id: str) -> str:
        row = self.file_for(ctx, attachment_id)
        return f"# {row.filename} ({row.media_type})\n\n{read_text(row)}"

    def create(self, ctx: ToolContext, filename: str, content: str, media_type: str = "") -> dict:
        data = content.encode()
        self._check_size(data)
        row = save_session_file(
            ctx.session, filename, data, media_type=media_type, source="bot"
        )
        if not is_text(row.media_type):
            row.file.delete(save=False)
            row.delete()
            msg = f"Only text files can be written ({row.media_type} is not text)"
            raise ValueError(msg)
        return describe_row(row)

    def update(self, ctx: ToolContext, attachment_id: str, content: str) -> dict:
        row = self.file_for(ctx, attachment_id)
        if row.session_id != ctx.session.id:
            msg = "Files in other sessions are read-only."
            raise ValueError(msg)
        if not is_text(row.media_type):
            msg = f"{row.filename} is not a text file"
            raise ValueError(msg)
        data = content.encode()
        self._check_size(data)
        return describe_row(replace_session_file(row, data))

    def _check_size(self, data: bytes) -> None:
        if len(data) > self.max_bytes:
            msg = f"File too large ({len(data)} bytes; the limit is {self.max_bytes})"
            raise ValueError(msg)

    # -- plugin hooks ----------------------------------------------------------

    def toolkits(self, ctx: ToolContext) -> list[Toolkit]:
        return [FunctionToolkit(self._tools(), ctx)]

    def context_sources(self, ctx: ToolContext, message: str) -> list[ContextSource]:
        def listing() -> str:
            rows = list(ctx.session.attachments.order_by("-updated_at")[:MAX_LISTED])
            if not rows:
                return ""
            lines = [
                f"- {r.filename} ({r.media_type}, {r.size or 0} bytes, {r.source}) id={r.id}"
                for r in rows
            ]
            return (
                "Read one with ergo_attachments_read; write with "
                "ergo_attachments_create or ergo_attachments_update.\n" + "\n".join(lines)
            )

        return [TextContextSource("Files in this chat", listing)]

    def _tools(self) -> list[BotTool]:
        plugin = self

        @bot_tool(name="ergo_attachments_list", takes_context=True)
        def list_files(ctx: ToolContext, session_id: str = "") -> list[dict]:
            """List the files in this chat session, or in another of the user's sessions by id."""
            return plugin.list_files(ctx, session_id)

        @bot_tool(name="ergo_attachments_read", takes_context=True)
        def read(ctx: ToolContext, attachment_id: str) -> str:
            """Read a file by id: its text, or a description of an image, audio clip or binary file."""
            return plugin.read(ctx, attachment_id)

        @bot_tool(
            name="ergo_attachments_create",
            takes_context=True,
            description="Write a new text file (Markdown, CSV, JSON...) into this chat session.",
            parameters={
                "filename": {"type": "string", "description": "e.g. plan.md"},
                "content": {"type": "string"},
                "media_type": {
                    "type": "string",
                    "description": "Optional; guessed from the filename",
                },
            },
            required=["filename", "content"],
        )
        def create(ctx: ToolContext, filename: str, content: str, media_type: str = "") -> dict:
            return plugin.create(ctx, filename, content, media_type)

        @bot_tool(name="ergo_attachments_update", takes_context=True)
        def update(ctx: ToolContext, attachment_id: str, content: str) -> dict:
            """Replace the whole contents of a text file in this chat session."""
            return plugin.update(ctx, attachment_id, content)

        return [fn.__bot_tool__ for fn in (list_files, read, create, update)]
