"""Attachments plugin: files in chat sessions that the bot can read and write.

    plugins:
      - name: attachments
        max_bytes: 5000000         # largest file the bot may write
        other_sessions: true       # may read files in the user's other sessions

Files come from three places: sent with a message, uploaded to the session
(Ergonaut's Files panel), or written by the bot. Tools:

- ``ergo_attachments_list``: the files in this session, or in another of
  the user's sessions (archived files only with ``include_archived``).
- ``ergo_attachments_read``: a file's text (or a description of an image,
  audio clip or binary file).
- ``ergo_attachments_create``: write a new text file into this session.
- ``ergo_attachments_update``: replace the contents of a text file in this
  session.
- ``ergo_attachments_look``: look at an image or PDF (or any file). An
  image comes back in the tool result, so the bot sees it itself
  (downscaled; see ``django_ergo.conversation.images``). Other files go to
  the bot's own model as an attachment in a separate call (kind
  ``attachment_look``) that answers the question about them.
- ``ergo_attachments_archive``: archive files in this session, by id or all
  of them (optionally only those older than N days, or all but the latest
  K), to clear old files out of the bot's working set.
- ``ergo_attachments_unarchive``: bring archived files back.

An archived file (``archived_at`` set) stays stored and readable by id, but
drops out of the default list and of the file list in the bot's context.
Archiving a file sent with a message doesn't change the message history.
Archiving is reversible, so neither tool asks for approval.

The bot only writes to its own session. It reads other sessions only when
they belong to the same user. Every turn's context lists the session's
files, so the model knows when something was uploaded.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

from django.utils import timezone

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
    from django_ergo.conversation.images import ToolResult
    from django_ergo.conversation.models import ConversationAttachment
    from django_ergo.conversation.models import ConversationSession
    from django_ergo.conversation.toolkit import Toolkit

MAX_LISTED = 50


def describe_row(row: ConversationAttachment) -> dict:
    out = {
        "id": str(row.id),
        "filename": row.filename,
        "media_type": row.media_type,
        "size": row.size,
        "source": row.source,
        "updated_at": row.updated_at.isoformat(timespec="seconds"),
    }
    if row.archived_at is not None:
        out["archived_at"] = row.archived_at.isoformat(timespec="seconds")
    return out


def _checked_ids(attachment_ids: list[str]) -> list[str]:
    """Normalized ids, or a ValueError naming the ones that aren't file ids."""
    ids, bad = [], []
    for raw in attachment_ids:
        try:
            ids.append(str(uuid.UUID(str(raw))))
        except ValueError:
            bad.append(str(raw))
    if bad:
        msg = f"No file {', '.join(bad)} in this chat"
        raise ValueError(msg)
    return ids


class AttachmentsPlugin(BotPlugin):
    name = "attachments"
    description = "Read, look at and write files in chats (images and PDFs too)"

    def on_load(self) -> None:
        self.max_bytes = int(self.config.get("max_bytes", 5_000_000))
        self.other_sessions = bool(self.config.get("other_sessions", True))
        self.max_look_bytes = int(self.config.get("max_look_bytes", 20_000_000))

    # -- access --------------------------------------------------------------

    def session_for(
        self, ctx: ToolContext, session_id: str = ""
    ) -> ConversationSession:
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

    def list_files(
        self, ctx: ToolContext, session_id: str = "", include_archived: bool = False
    ) -> list[dict]:
        session = self.session_for(ctx, session_id)
        rows = session.attachments.all()
        if not include_archived:
            rows = rows.filter(archived_at__isnull=True)
        return [describe_row(r) for r in rows.order_by("-updated_at")[:MAX_LISTED]]

    def read(self, ctx: ToolContext, attachment_id: str) -> str:
        row = self.file_for(ctx, attachment_id)
        text = f"# {row.filename} ({row.media_type})\n\n{read_text(row)}"
        if not is_text(row.media_type):
            text += "\n\nThis isn't a text file; use ergo_attachments_look to see what's in it."
        return text

    def create(
        self, ctx: ToolContext, filename: str, content: str, media_type: str = ""
    ) -> dict:
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

    def look(
        self, ctx: ToolContext, attachment_id: str, question: str = ""
    ) -> str | ToolResult:
        from asgiref.sync import async_to_sync

        from django_ergo.conversation.attachments import Attachment
        from django_ergo.conversation.images import ToolImage
        from django_ergo.conversation.images import ToolResult
        from django_ergo.conversation.structured import StructuredCallSpec
        from django_ergo.conversation.structured import run_structured_call

        row = self.file_for(ctx, attachment_id)
        if is_text(row.media_type):
            return self.read(ctx, attachment_id)
        if not row.file:
            msg = f"{row.filename} has no stored copy to look at"
            raise ValueError(msg)
        if (row.size or 0) > self.max_look_bytes:
            msg = f"{row.filename} is too large to look at ({row.size} bytes)"
            raise ValueError(msg)
        if row.kind == "image":
            # The bot sees the image itself, in this tool result.
            text = f"{row.filename} ({row.media_type}, id={row.id})"
            if question:
                text += f"\nYou wanted to know: {question}"
            return ToolResult(text, [ToolImage.from_attachment(row)])
        with row.file.open("rb") as handle:
            data = handle.read()
        attachment = Attachment(
            media_type=row.media_type,
            data=data,
            filename=row.filename,
            transcript=row.transcript,
            kind=row.kind,
        )
        spec = StructuredCallSpec(
            kind="attachment_look",
            system_prompt=(
                "You are looking at a file for another assistant. Answer its question "
                "about the attached file accurately and concisely. If the file is a "
                "document, quote the relevant parts; if it is an image, describe what "
                "matters for the question."
            ),
            output_parser=str,
            max_turns=1,
        )
        result = async_to_sync(run_structured_call)(
            spec,
            question or "Describe this file in detail.",
            user=ctx.user,
            engine=self.bot.make_engine(),
            attachments=[attachment],
            metadata={"attachment": str(row.id), "session": str(ctx.session.id)},
        )
        if result.parsed is None:
            msg = (
                f"Could not look at {row.filename}: {result.call.error or 'no answer'}"
            )
            raise ValueError(msg)
        return f"{row.filename}: {result.parsed}"

    def archive(
        self,
        ctx: ToolContext,
        attachment_ids: list[str] | None = None,
        all_files: bool = False,
        older_than_days: float = 0,
        keep_latest: int = 0,
    ) -> dict:
        """Archive files in this session: the given ids, or all of them.

        ``older_than_days`` and ``keep_latest`` narrow either selection: only
        files not updated for that many days, and never the ``keep_latest``
        most recently updated files that are still active.
        """
        files = ctx.session.attachments
        active = files.filter(archived_at__isnull=True)
        if attachment_ids:
            attachment_ids = _checked_ids(attachment_ids)
            found = {
                str(i)
                for i in files.filter(id__in=attachment_ids).values_list(
                    "id", flat=True
                )
            }
            missing = [i for i in attachment_ids if i not in found]
            if missing:
                msg = f"No file {', '.join(missing)} in this chat"
                raise ValueError(msg)
            chosen = active.filter(id__in=attachment_ids)
        elif all_files:
            chosen = active
        else:
            msg = "Give attachment_ids, or all_files=true to archive every file"
            raise ValueError(msg)
        if older_than_days and older_than_days > 0:
            cutoff = timezone.now() - timedelta(days=older_than_days)
            chosen = chosen.filter(updated_at__lt=cutoff)
        if keep_latest and keep_latest > 0:
            latest = active.order_by("-updated_at").values_list("id", flat=True)
            chosen = chosen.exclude(id__in=list(latest[:keep_latest]))
        ids = list(chosen.values_list("id", flat=True))
        # update() leaves updated_at alone, so a file's age survives archiving.
        files.filter(id__in=ids).update(archived_at=timezone.now())
        rows = files.filter(id__in=ids).order_by("-updated_at")
        return {
            "archived": [describe_row(r) for r in rows],
            "remaining": active.count(),
        }

    def unarchive(self, ctx: ToolContext, attachment_ids: list[str]) -> dict:
        """Bring archived files in this session back into the file list."""
        if not attachment_ids:
            msg = "Give the attachment_ids to unarchive"
            raise ValueError(msg)
        attachment_ids = _checked_ids(attachment_ids)
        files = ctx.session.attachments
        ids = list(
            files.filter(id__in=attachment_ids, archived_at__isnull=False).values_list(
                "id", flat=True
            )
        )
        files.filter(id__in=ids).update(archived_at=None)
        back = files.filter(id__in=ids).order_by("-updated_at")
        return {"unarchived": [describe_row(r) for r in back]}

    def _check_size(self, data: bytes) -> None:
        if len(data) > self.max_bytes:
            msg = f"File too large ({len(data)} bytes; the limit is {self.max_bytes})"
            raise ValueError(msg)

    # -- plugin hooks ----------------------------------------------------------

    def toolkits(self, ctx: ToolContext) -> list[Toolkit]:
        return [FunctionToolkit(self._tools(), ctx)]

    def skill_hint(self, ctx: ToolContext) -> str:
        count = (
            ctx.session.attachments.filter(archived_at__isnull=True).count()
            if ctx.session is not None
            else 0
        )
        if not count:
            return ""
        return f"{count} file{'s' if count != 1 else ''} in this chat"

    def context_sources(self, ctx: ToolContext, message: str) -> list[ContextSource]:
        def listing() -> str:
            files = ctx.session.attachments
            active = files.filter(archived_at__isnull=True).order_by("-updated_at")
            rows = list(active[:MAX_LISTED])
            archived = files.filter(archived_at__isnull=False).count()
            if not rows and not archived:
                return ""
            lines = [
                f"- {r.filename} ({r.media_type}, {r.size or 0} bytes, {r.source}) id={r.id}"
                for r in rows
            ]
            if archived:
                lines.append(
                    f"({archived} archived file{'s' if archived != 1 else ''} not "
                    "listed; ergo_attachments_list with include_archived shows them)"
                )
            return (
                "Read text with ergo_attachments_read, look at images and PDFs with "
                "ergo_attachments_look; write with "
                "ergo_attachments_create or ergo_attachments_update; clear old "
                "files out of this list with ergo_attachments_archive.\n"
                + "\n".join(lines)
            )

        return [TextContextSource("Files in this chat", listing)]

    def _tools(self) -> list[BotTool]:
        plugin = self

        @bot_tool(
            name="ergo_attachments_list",
            takes_context=True,
            description=(
                "List the files in this chat session, or in another of the user's "
                "sessions by id. Archived files are left out unless include_archived."
            ),
            parameters={
                "session_id": {
                    "type": "string",
                    "description": "Another of the user's sessions (default: this one)",
                },
                "include_archived": {
                    "type": "boolean",
                    "description": "Also list archived files (default false)",
                },
            },
            required=[],
        )
        def list_files(
            ctx: ToolContext,
            session_id: str = "",
            include_archived: bool = False,
        ) -> list[dict]:
            return plugin.list_files(ctx, session_id, include_archived)

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
        def create(
            ctx: ToolContext, filename: str, content: str, media_type: str = ""
        ) -> dict:
            return plugin.create(ctx, filename, content, media_type)

        @bot_tool(name="ergo_attachments_update", takes_context=True)
        def update(ctx: ToolContext, attachment_id: str, content: str) -> dict:
            """Replace the whole contents of a text file in this chat session."""
            return plugin.update(ctx, attachment_id, content)

        @bot_tool(
            name="ergo_attachments_look",
            takes_context=True,
            description=(
                "Look at an image or PDF (or any file) by id. You see an image "
                "yourself in the result; for a PDF or other file, the question is "
                "answered for you, e.g. what a receipt says."
            ),
            parameters={
                "attachment_id": {"type": "string"},
                "question": {
                    "type": "string",
                    "description": "What you want to know (default: describe it)",
                },
            },
            required=["attachment_id"],
        )
        def look(
            ctx: ToolContext, attachment_id: str, question: str = ""
        ) -> str | ToolResult:
            return plugin.look(ctx, attachment_id, question)

        @bot_tool(
            name="ergo_attachments_archive",
            takes_context=True,
            description=(
                "Archive files in this chat session to clear them out of your "
                "working set: they leave the file list in your context and the "
                "default ergo_attachments_list, but stay stored, readable by id, "
                "and can be unarchived. Give attachment_ids, or all_files=true; "
                "older_than_days and keep_latest narrow either choice."
            ),
            parameters={
                "attachment_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Files to archive",
                },
                "all_files": {
                    "type": "boolean",
                    "description": "Archive every file in this chat",
                },
                "older_than_days": {
                    "type": "number",
                    "description": "Only files not updated for this many days",
                },
                "keep_latest": {
                    "type": "integer",
                    "description": "Keep this many of the most recently updated files",
                },
            },
            required=[],
        )
        def archive(
            ctx: ToolContext,
            attachment_ids: list[str] | None = None,
            all_files: bool = False,
            older_than_days: float = 0,
            keep_latest: int = 0,
        ) -> dict:
            return plugin.archive(
                ctx, attachment_ids, all_files, older_than_days, keep_latest
            )

        @bot_tool(
            name="ergo_attachments_unarchive",
            takes_context=True,
            description="Bring archived files in this chat session back into the file list.",
            parameters={
                "attachment_ids": {"type": "array", "items": {"type": "string"}},
            },
        )
        def unarchive(ctx: ToolContext, attachment_ids: list[str]) -> dict:
            return plugin.unarchive(ctx, attachment_ids)

        return [
            fn.__bot_tool__
            for fn in (list_files, read, create, update, look, archive, unarchive)
        ]
