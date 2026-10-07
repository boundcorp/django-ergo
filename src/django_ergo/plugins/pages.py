"""Pages plugin: the bot writes live pages (``.jhtml``) over its tables, from blocks.

    plugins:
      - name: pages

A page is a ``.jhtml`` file (see django_ergo.bots.pages): Jinja over the
bot's tables, rendered whenever someone opens it. The bot writes pages into
the chat with ``ergo_page_write``, from a list of blocks (heading, markdown,
metric, form, table, button, chart, html) or as Jinja source, and pins them so
they show in the chat's pinned files. Pages that belong in the bot repo
(``pages/dashboard.jhtml``, pinned with ``chats.<name>.pins``) are proposed
like any other file; ``ergo_page_preview`` renders either kind so the bot can
check its work.

Tools:

- ``ergo_page_write``: write (or rewrite, by filename) a page in this chat.
- ``ergo_page_get``: a page's blocks and source, to change it.
- ``ergo_page_preview``: render a page (a chat file, a bot-folder path, or
  source) and return its text, or the error.
- ``ergo_page_pin``: pin or unpin any file in this chat.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from django_ergo.bots.pages import BLOCK_TYPES
from django_ergo.bots.pages import PageError
from django_ergo.bots.pages import blocks_source
from django_ergo.bots.pages import page_text
from django_ergo.bots.pages import render_page
from django_ergo.bots.plugins import BotPlugin
from django_ergo.bots.tools import FunctionToolkit
from django_ergo.bots.tools import bot_tool

if TYPE_CHECKING:
    from django_ergo.bots.tools import BotTool
    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.models import ConversationAttachment
    from django_ergo.conversation.toolkit import Toolkit

INSTRUCTIONS = """\
Pages are live: a .jhtml page is a Jinja template rendered over this bot's tables every time
someone opens it, so it always shows current data. Write one with ergo_page_write and check it
with ergo_page_preview (it shows the rendered text or the error). Pinned pages show at the top of
the chat.

Easiest: give blocks, a list of objects with a "type":
{block_types}

Example blocks:
[{{"type": "metric", "label": "Spend (7 days)", "table": "AdStat", "aggregate": "sum", "field": "spend",
   "filters": {{"date__gte": "2026-09-25"}}, "format": "money"}},
 {{"type": "chart", "title": "Daily spend", "table": "AdStat", "x": "date", "y": "spend", "group": "campaign"}},
 {{"type": "table", "table": "AdStat", "columns": ["date", "campaign", "spend", "clicks"],
   "order_by": ["-date"], "limit": 20}}]
Filters use Django lookups (field__gte, field__icontains, ...).

Or write Jinja source (sandboxed, read-only). Available in a page:
- table("Name") is a read-only view: .filter(**lookups), .exclude(...), .order_by("-date"), .limit(n),
  .count(), .sum("f"), .avg("f"), .min("f"), .max("f"), .first(), .rows(), and
  .group("campaign", spend="sum", clicks="sum", n="count") for one row per value. Loop over a view
  to get rows (row.field).
- blocks.metric(...), blocks.form(...), blocks.chart(...), blocks.table(...), blocks.button(...),
  blocks.markdown(text=...) take the block fields as arguments. Forms and editable
  tables write a table; buttons invoke a page action or use ``ask=`` to send a chat
  message through ``ergo.ask``.
- now, today, days_ago(n), user.username, bot.name; filters money, number, percent, markdown, tojson.
- {{% include "pages/part.jhtml" %}} loads a file from the bot folder.
A page without an <html> tag gets a layout with styles and Chart.js; write a full document to
control everything (Chart.js is at {chart_js}).
"""


class PagesPlugin(BotPlugin):
    name = "pages"
    description = "Write live dashboard pages (.jhtml) over this bot's tables, and pin them in the chat"

    def on_load(self) -> None:
        self.max_bytes = int(self.config.get("max_bytes", 500_000))

    @property
    def skill_instructions(self) -> str:
        from django_ergo.bots.pages import CHART_JS

        types = "\n".join(f"- {name}: {fields}" for name, fields in BLOCK_TYPES.items())
        return INSTRUCTIONS.format(block_types=types, chart_js=CHART_JS)

    @property
    def skill_requires(self) -> list[str]:
        return ["tables"] if self.bot.tables else []

    # -- pages ----------------------------------------------------------------

    def page_row(self, ctx: ToolContext, attachment_id: str) -> ConversationAttachment:
        from django_ergo.conversation.models import ConversationAttachment

        row = ConversationAttachment.objects.filter(
            id=attachment_id, session=ctx.session
        ).first()
        if row is None:
            msg = f"No file {attachment_id} in this chat"
            raise ValueError(msg)
        return row

    def render(self, ctx: ToolContext, source: str, title: str = "") -> str:
        return render_page(self.bot, source, user=ctx.user, title=title)

    def check(self, ctx: ToolContext, source: str, title: str = "") -> dict:
        try:
            return {
                "ok": True,
                "preview": page_text(self.render(ctx, source, title), 1500),
            }
        except PageError as exc:
            return {"ok": False, "error": str(exc)}

    def write(  # noqa: PLR0913
        self,
        ctx: ToolContext,
        filename: str,
        title: str = "",
        blocks: list | None = None,
        source: str = "",
        pin: bool = True,
    ) -> dict:
        from django_ergo.conversation.attachments import replace_session_file
        from django_ergo.conversation.attachments import save_session_file

        if not filename.endswith(".jhtml"):
            filename += ".jhtml"
        if blocks and source:
            msg = "Give blocks or source, not both"
            raise ValueError(msg)
        if blocks is not None:
            source = blocks_source(title, list(blocks))
        if not source.strip():
            msg = "A page needs blocks or source"
            raise ValueError(msg)
        data = source.encode()
        if len(data) > self.max_bytes:
            msg = f"Page too large ({len(data)} bytes; the limit is {self.max_bytes})"
            raise ValueError(msg)
        metadata = {"title": title, "blocks": blocks, "pinned": bool(pin)}
        row = (
            ctx.session.attachments.filter(
                filename=filename, source="bot", message_sequence__isnull=True
            )
            .order_by("-updated_at")
            .first()
        )
        if row is None:
            row = save_session_file(
                ctx.session,
                filename,
                data,
                media_type="text/x-jhtml",
                source="bot",
                metadata=metadata,
            )
        else:
            row.metadata = {**(row.metadata or {}), **metadata}
            row = replace_session_file(row, data)
        return {
            "id": str(row.id),
            "filename": row.filename,
            "pinned": bool(pin),
            **self.check(ctx, source, title),
        }

    def get(self, ctx: ToolContext, attachment_id: str) -> dict:
        from django_ergo.conversation.attachments import read_text

        row = self.page_row(ctx, attachment_id)
        meta = row.metadata or {}
        return {
            "filename": row.filename,
            "title": meta.get("title", ""),
            "blocks": meta.get("blocks"),
            "source": read_text(row),
        }

    def preview(self, ctx: ToolContext, page: str = "", source: str = "") -> dict:
        from django_ergo.conversation.attachments import read_text

        title = ""
        if source:
            pass
        elif not page:
            msg = "Give a page (an id or a bot-folder path) or source"
            raise ValueError(msg)
        elif not _is_uuid(page):
            root = self.bot.definition.root_dir
            path = (root / page).resolve() if root else None
            if (
                path is None
                or not path.is_relative_to(root.resolve())
                or not path.is_file()
            ):
                msg = f"No page {page} in the bot folder"
                raise ValueError(msg)
            source = path.read_text()
        else:
            row = self.page_row(ctx, page)
            source, title = read_text(row), (row.metadata or {}).get("title", "")
        return self.check(ctx, source, title)

    def pin(
        self,
        ctx: ToolContext,
        attachment_id: str,
        pinned: bool = True,
        title: str = "",
        icon: str = "",
    ) -> str:
        row = self.page_row(ctx, attachment_id)
        labels = {
            k: v.strip() for k, v in (("title", title), ("icon", icon)) if v.strip()
        }
        row.metadata = {**(row.metadata or {}), **labels, "pinned": bool(pinned)}
        row.save(update_fields=["metadata", "updated_at"])
        return f"{'Pinned' if pinned else 'Unpinned'} {row.filename}"

    # -- plugin hooks ----------------------------------------------------------

    def toolkits(self, ctx: ToolContext) -> list[Toolkit]:
        return [FunctionToolkit(self._tools(), ctx)]

    def skill_hint(self, ctx: ToolContext) -> str:
        if ctx.session is None:
            return ""
        count = ctx.session.attachments.filter(filename__endswith=".jhtml").count()
        return f"{count} page{'s' if count != 1 else ''} in this chat" if count else ""

    def _tools(self) -> list[BotTool]:
        plugin = self

        @bot_tool(
            name="ergo_page_write",
            takes_context=True,
            description=(
                "Write a live page (.jhtml) into this chat from blocks or Jinja source; writing the same "
                "filename again replaces it. Returns the page's id and a preview of the render (or the error)."
            ),
            parameters={
                "filename": {
                    "type": "string",
                    "description": "e.g. ad-dashboard.jhtml",
                },
                "title": {"type": "string"},
                "blocks": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Blocks, each with a type: "
                    + ", ".join(BLOCK_TYPES),
                },
                "source": {
                    "type": "string",
                    "description": "Jinja/HTML source instead of blocks",
                },
                "pin": {
                    "type": "boolean",
                    "description": "Pin it in the chat (default true)",
                },
            },
            required=["filename"],
        )
        def write(  # noqa: PLR0913
            ctx: ToolContext,
            filename: str,
            title: str = "",
            blocks: list | None = None,
            source: str = "",
            pin: bool = True,
        ) -> dict:
            return plugin.write(ctx, filename, title, blocks, source, pin)

        @bot_tool(name="ergo_page_get", takes_context=True)
        def get(ctx: ToolContext, attachment_id: str) -> dict:
            """A page in this chat: its title, blocks (if written from blocks) and source."""
            return plugin.get(ctx, attachment_id)

        @bot_tool(
            name="ergo_page_preview",
            takes_context=True,
            description=(
                "Render a page and return its text, or the error: a page in this chat (by id), a page in "
                "the bot folder (by path, e.g. pages/dashboard.jhtml), or source."
            ),
            parameters={
                "page": {
                    "type": "string",
                    "description": "A chat file id or a bot-folder path",
                },
                "source": {
                    "type": "string",
                    "description": "Jinja source to try instead",
                },
            },
            required=[],
        )
        def preview(ctx: ToolContext, page: str = "", source: str = "") -> dict:
            return plugin.preview(ctx, page, source)

        @bot_tool(
            name="ergo_page_pin",
            takes_context=True,
            description=(
                "Pin a file in this chat (it shows at the top and in the sidebar), or unpin it with "
                "pinned=false. title and icon (an emoji) label the pin; they default to the file name "
                "and an icon for its type."
            ),
            parameters={
                "attachment_id": {"type": "string"},
                "pinned": {"type": "boolean"},
                "title": {"type": "string"},
                "icon": {"type": "string", "description": "An emoji"},
            },
            required=["attachment_id"],
        )
        def pin(
            ctx: ToolContext,
            attachment_id: str,
            pinned: bool = True,
            title: str = "",
            icon: str = "",
        ) -> str:
            return plugin.pin(ctx, attachment_id, pinned, title, icon)

        return [fn.__bot_tool__ for fn in (write, get, preview, pin)]


def _is_uuid(value: Any) -> bool:
    import uuid

    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True
