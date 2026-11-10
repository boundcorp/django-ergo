"""Attachments plugin: files in chat sessions that the bot can read and write.

    plugins:
      - name: attachments
        max_bytes: 5000000         # largest file the bot may write
        other_sessions: true       # may read files in the user's other sessions
        video_model: gemini-flash-latest   # answers questions about videos
        gemini_api_key_env: GEMINI_API_KEY
        max_video_bytes: 2000000000        # largest video sent to Gemini

Files come from three places: sent with a message, uploaded to the session
(Ergonaut's Files panel), or written by the bot. Tools:

- ``ergo_attachments_list``: the files in this session, or in another of
  the user's sessions (archived files only with ``include_archived``).
- ``ergo_attachments_read``: a file's text (or a description of an image,
  audio clip or binary file). Read and look take a file id, or a filename in
  this chat or another (``session_id``), e.g. files another chat shared with
  a message (see ``django_ergo.bots.orchestrator``).
- ``ergo_attachments_create``: write a new text file into this session.
- ``ergo_attachments_update``: replace the contents of a text file in this
  session.
- ``ergo_attachments_look``: look at an image, PDF or video (or any file).
  An image comes back in the tool result, so the bot sees it itself
  (downscaled; see ``django_ergo.conversation.images``). A video goes to
  Gemini, which watches it (picture and sound, optionally only from
  ``start_seconds`` to ``end_seconds``) and answers the question; it needs
  the Gemini key. Other files go to the bot's own model as an attachment in a
  separate call (kind ``attachment_look``) that answers the question.
- ``ergo_attachments_frames``: still frames from a video, evenly spaced or
  at given seconds, which the bot sees itself and which are saved in the
  chat. Needs ffmpeg, not Gemini (see ``django_ergo.conversation.video``).
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
from django_ergo.conversation.attachments import model_readable
from django_ergo.conversation.attachments import read_text
from django_ergo.conversation.attachments import replace_session_file
from django_ergo.conversation.attachments import save_session_file
from django_ergo.conversation.context import TextContextSource
from django_ergo.conversation.video import is_video

if TYPE_CHECKING:
    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.context import ContextSource
    from django_ergo.conversation.images import ToolResult
    from django_ergo.conversation.models import ConversationAttachment
    from django_ergo.conversation.models import ConversationSession
    from django_ergo.conversation.toolkit import Toolkit

MAX_LISTED = 50
# Frames per ergo_attachments_frames call: the newest round of tool results
# carries at most this many images (images.ROUND_IMAGES_MAX).
MAX_FRAMES = 8
WATCH_PROMPT = (
    "You are watching a video for another assistant that can't see video. "
    "Answer its question accurately and concretely. Give timestamps (m:ss) for "
    "what you describe, quote on-screen text and speech exactly, and mention the "
    "audio (voiceover, music, sound effects) when it matters."
)


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
        self.video_model = str(self.config.get("video_model") or "")
        self.gemini_key_env = str(
            self.config.get("gemini_api_key_env") or "GEMINI_API_KEY"
        )
        self.max_video_bytes = int(self.config.get("max_video_bytes", 2_000_000_000))

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

    def file_for(
        self, ctx: ToolContext, attachment_id: str, session_id: str = ""
    ) -> ConversationAttachment:
        """A file by id, or by filename in this chat (or ``session_id``'s)."""
        from django_ergo.conversation.attachments import find_session_file
        from django_ergo.conversation.models import ConversationAttachment

        try:
            uuid.UUID(str(attachment_id))
        except ValueError:
            return find_session_file(self.session_for(ctx, session_id), attachment_id)
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

    def read(self, ctx: ToolContext, attachment_id: str, session_id: str = "") -> str:
        row = self.file_for(ctx, attachment_id, session_id)
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

    def look(  # noqa: PLR0913
        self,
        ctx: ToolContext,
        attachment_id: str,
        question: str = "",
        session_id: str = "",
        start_seconds: float | None = None,
        end_seconds: float | None = None,
    ) -> str | ToolResult:
        from asgiref.sync import async_to_sync

        from django_ergo.conversation.attachments import Attachment
        from django_ergo.conversation.images import ToolImage
        from django_ergo.conversation.images import ToolResult
        from django_ergo.conversation.structured import StructuredCallSpec
        from django_ergo.conversation.structured import run_structured_call

        row = self.file_for(ctx, attachment_id, session_id)
        if is_text(row.media_type):
            return self.read(ctx, attachment_id)
        if not row.file:
            msg = f"{row.filename} has no stored copy to look at"
            raise ValueError(msg)
        if is_video(row.media_type, row.filename):
            return self.watch(ctx, row, question, start_seconds, end_seconds)
        if not model_readable(row):
            # An archive would reach the model as a text stand-in only.
            msg = (
                f"{row.filename} ({row.media_type or 'unknown type'}) can't be looked at: "
                "the model only sees images, PDFs, videos and text. Copy it somewhere a "
                "tool can process it and look at the results instead."
            )
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

    def watch(
        self,
        ctx: ToolContext,
        row: ConversationAttachment,
        question: str = "",
        start_seconds: float | None = None,
        end_seconds: float | None = None,
    ) -> str:
        """Ask Gemini about a video file."""
        from django_ergo.conversation.video import VideoError
        from django_ergo.conversation.video import ask_gemini
        from django_ergo.conversation.video import local_path
        from django_ergo.conversation.video import video_media_type

        key = ctx.secret(self.gemini_key_env)
        if not key:
            msg = (
                f"Watching {row.filename} needs {self.gemini_key_env}, which isn't set "
                "here. Use ergo_attachments_frames to see still frames instead."
            )
            raise ValueError(msg)
        if (row.size or 0) > self.max_video_bytes:
            msg = f"{row.filename} is too large to watch ({row.size} bytes)"
            raise ValueError(msg)
        try:
            with local_path(row) as path:
                answer = ask_gemini(
                    path,
                    video_media_type(row.media_type, row.filename),
                    question
                    or "Describe this video in detail, scene by scene with timestamps, "
                    "including any on-screen text, voiceover and music.",
                    api_key=key,
                    model=self.video_model,
                    system=WATCH_PROMPT,
                    start=start_seconds,
                    end=end_seconds,
                )
        except VideoError as exc:
            msg = f"Could not watch {row.filename}: {exc}"
            raise ValueError(msg) from exc
        span = ""
        if start_seconds is not None or end_seconds is not None:
            span = f" ({start_seconds or 0:g}s to {'end' if end_seconds is None else f'{end_seconds:g}s'})"
        return f"{row.filename}{span}: {answer.text}\n\n[{answer.usage_line()}]"

    def frames(  # noqa: PLR0913
        self,
        ctx: ToolContext,
        attachment_id: str,
        count: int = 6,
        times: list[float] | None = None,
        session_id: str = "",
        width: int = 768,
    ) -> ToolResult:
        """Still frames from a video, as images the bot sees itself."""
        from django_ergo.conversation.images import ToolImage
        from django_ergo.conversation.images import ToolResult
        from django_ergo.conversation.video import VideoError
        from django_ergo.conversation.video import even_times
        from django_ergo.conversation.video import extract_frames
        from django_ergo.conversation.video import local_path
        from django_ergo.conversation.video import probe
        from django_ergo.conversation.video import timestamp

        row = self.file_for(ctx, attachment_id, session_id)
        if not is_video(row.media_type, row.filename):
            msg = f"{row.filename} ({row.media_type}) isn't a video"
            raise ValueError(msg)
        width = max(160, min(int(width or 768), 1280))
        try:
            with local_path(row) as path:
                info = probe(path)
                if times:
                    # The last frame can end well before the audio does.
                    last = max(info.duration - 0.5, 0)
                    wanted = sorted({min(round(float(t), 2), last) for t in times})
                    wanted = wanted[:MAX_FRAMES]
                else:
                    wanted = even_times(
                        info.duration, max(1, min(int(count or 6), MAX_FRAMES))
                    )
                shots = extract_frames(path, wanted, width)
        except VideoError as exc:
            msg = f"Could not get frames from {row.filename}: {exc}"
            raise ValueError(msg) from exc
        stem = row.filename.rsplit(".", 1)[0] or "video"
        images = [
            ToolImage(
                data=jpeg,
                media_type="image/jpeg",
                name=f"{stem}@{at:06.2f}s.jpg",
            )
            for at, jpeg in shots
        ]
        lines = [f"{row.filename}: {info.describe()}. Frames at:"]
        lines += [f"- {timestamp(at)} ({at:g}s)" for at, _ in shots]
        return ToolResult("\n".join(lines), images)

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
                "Read text with ergo_attachments_read, look at images, PDFs and "
                "videos with ergo_attachments_look (ergo_attachments_frames for "
                "stills from a video); write with "
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

        @bot_tool(
            name="ergo_attachments_read",
            takes_context=True,
            description=(
                "Read a file: its text, or a description of an image, audio clip or "
                "binary file. By id, or by filename in this chat or another (session_id)."
            ),
            parameters={
                "attachment_id": {
                    "type": "string",
                    "description": "File id or filename",
                },
                "session_id": {
                    "type": "string",
                    "description": "The chat a filename is in (default: this one)",
                },
            },
            required=["attachment_id"],
        )
        def read(ctx: ToolContext, attachment_id: str, session_id: str = "") -> str:
            return plugin.read(ctx, attachment_id, session_id)

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
                "Look at an image, PDF or video (or any file) by id, or by filename in "
                "this chat or another (session_id). You see an image yourself in the "
                "result; for a PDF, video or other file, the question is answered for "
                "you, e.g. what a receipt says or what happens in an ad and when. A "
                "video is watched with its sound; start_seconds/end_seconds limit it "
                "to part of the video."
            ),
            parameters={
                "attachment_id": {
                    "type": "string",
                    "description": "File id or filename",
                },
                "session_id": {
                    "type": "string",
                    "description": "The chat a filename is in (default: this one)",
                },
                "question": {
                    "type": "string",
                    "description": "What you want to know (default: describe it)",
                },
                "start_seconds": {
                    "type": "number",
                    "description": "Video only: watch from here (default: the start)",
                },
                "end_seconds": {
                    "type": "number",
                    "description": "Video only: watch up to here (default: the end)",
                },
            },
            required=["attachment_id"],
        )
        def look(  # noqa: PLR0913
            ctx: ToolContext,
            attachment_id: str,
            question: str = "",
            session_id: str = "",
            start_seconds: float | None = None,
            end_seconds: float | None = None,
        ) -> str | ToolResult:
            return plugin.look(
                ctx, attachment_id, question, session_id, start_seconds, end_seconds
            )

        @bot_tool(
            name="ergo_attachments_frames",
            takes_context=True,
            description=(
                "See still frames from a video yourself: evenly spaced (count) or at "
                f"given seconds (times), up to {MAX_FRAMES}. The frames are saved in "
                "this chat, so you can attach or refer to them. Use "
                "ergo_attachments_look to have the whole video (with sound) watched."
            ),
            parameters={
                "attachment_id": {
                    "type": "string",
                    "description": "Video file id or filename",
                },
                "session_id": {
                    "type": "string",
                    "description": "The chat a filename is in (default: this one)",
                },
                "count": {
                    "type": "integer",
                    "description": f"Evenly spaced frames (default 6, at most {MAX_FRAMES})",
                },
                "times": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Seconds to take frames at, instead of count",
                },
                "width": {
                    "type": "integer",
                    "description": "Frame width in pixels (default 768)",
                },
            },
            required=["attachment_id"],
        )
        def frames(  # noqa: PLR0913
            ctx: ToolContext,
            attachment_id: str,
            count: int = 6,
            times: list[float] | None = None,
            session_id: str = "",
            width: int = 768,
        ) -> ToolResult:
            return plugin.frames(ctx, attachment_id, count, times, session_id, width)

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
            for fn in (
                list_files,
                read,
                create,
                update,
                look,
                frames,
                archive,
                unarchive,
            )
        ]
