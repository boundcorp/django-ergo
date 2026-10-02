"""Live pages: ``.jhtml`` files are Jinja templates rendered over a bot's tables.

A page is either a file in the bot's folder (``pages/dashboard.jhtml``,
changed through the bot repo like any other file) or a file the bot wrote
into a chat (an attachment). Either way it renders when it's opened, so it
always shows the current rows.

    <h1>Ad spend</h1>
    <p>{{ table("AdStat").filter(date__gte=days_ago(7)).sum("spend") | money }} this week</p>
    {% for row in table("AdStat").order_by("-date").limit(10) %}
      <div>{{ row.date }} {{ row.campaign }} {{ row.spend | money }}</div>
    {% endfor %}

    {{ blocks.metric(label="Installs", table="AdStat", aggregate="sum", field="installs") }}
    {{ blocks.chart(table="AdStat", x="date", y="spend", group="campaign", kind="line") }}

Templates run in Jinja's sandbox and only read: ``table(name)`` gives a
read-only view (filter, exclude, order_by, limit, count, sum, avg, min, max,
group, first, rows), never a queryset. ``{% include %}`` and
``{% extends %}`` load other pages from the bot folder. A page without an
``<html>`` tag is wrapped in a plain layout that has Chart.js and styles for
the blocks.

``blocks`` (see ``BLOCK_TYPES``) are the building blocks the pages plugin
writes pages from; hand-written pages can use them too.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from django_ergo.bots.runtime import Bot

MAX_ROWS = 1000
PAGE_SUFFIXES = (".jhtml",)
CHART_JS = "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"


class PageError(Exception):
    """A page that couldn't render; the message says where and why."""


# -- read-only table views ------------------------------------------------------------


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dt.datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


class Row(dict):
    """A table row: ``row.field`` and ``row["field"]`` both work in templates."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class TableView:
    """A read-only, chainable view of one bot table for templates."""

    def __init__(self, model, qs=None, limit: int | None = None):
        # Underscored, so templates (Jinja's sandbox) can't reach the model or queryset.
        self._model = model
        self._qs = qs if qs is not None else model.objects.all()
        self._limit = limit

    @property
    def name(self) -> str:
        return self._model.__name__

    def _with(self, qs=None, limit: int | None = None) -> TableView:
        return TableView(
            self._model,
            self._qs if qs is None else qs,
            self._limit if limit is None else limit,
        )

    def filter(self, **lookups) -> TableView:
        return self._with(self._qs.filter(**lookups))

    def exclude(self, **lookups) -> TableView:
        return self._with(self._qs.exclude(**lookups))

    def order_by(self, *fields: str) -> TableView:
        return self._with(self._qs.order_by(*fields))

    def limit(self, n: int) -> TableView:
        return self._with(limit=max(0, min(int(n), MAX_ROWS)))

    def count(self) -> int:
        return self._qs.count()

    def _aggregate(self, fn, field: str):
        from django.db import models

        value = self._qs.aggregate(v=getattr(models, fn)(field))["v"]
        return _plain(value)

    def sum(self, field: str):
        # None when nothing was reported: unknown, not zero.
        return self._aggregate("Sum", field)

    def avg(self, field: str):
        return self._aggregate("Avg", field)

    def min(self, field: str):
        return self._aggregate("Min", field)

    def max(self, field: str):
        return self._aggregate("Max", field)

    def group(self, *fields: str, **aggregates: str) -> list[Row]:
        """Rows per distinct ``fields``, e.g. ``group("campaign", spend="sum", clicks="sum", n="count")``.

        Each aggregate is ``sum``, ``avg``, ``min``, ``max`` or ``count``;
        ``spend="sum"`` sums the field of that name, ``total="sum:spend"`` names it.
        """
        from django.db import models

        functions = {
            "sum": models.Sum,
            "avg": models.Avg,
            "min": models.Min,
            "max": models.Max,
            "count": models.Count,
        }
        annotations = {}
        for alias, spec in aggregates.items():
            fn, _, field = str(spec).partition(":")
            if fn not in functions:
                msg = f"group: {alias}={spec!r} must be one of {', '.join(functions)}"
                raise PageError(msg)
            annotations[alias] = functions[fn](
                field or (alias if fn != "count" else "pk")
            )
        qs = self._qs.values(*fields).annotate(**annotations).order_by(*fields)
        limit = self._limit or MAX_ROWS
        return [Row({k: _plain(v) for k, v in row.items()}) for row in qs[:limit]]

    def rows(self) -> list[Row]:
        from django_ergo.bots.tables import _row

        limit = self._limit or MAX_ROWS
        return [Row(_row(obj)) for obj in self._qs[:limit]]

    def first(self) -> Row | None:
        found = self.limit(1).rows()
        return found[0] if found else None

    def fields(self) -> list[str]:
        return [f.name for f in self._model._meta.concrete_fields]  # noqa: SLF001

    def __iter__(self):
        return iter(self.rows())

    def __len__(self) -> int:
        return len(self.rows())

    def __bool__(self) -> bool:
        return self._qs.exists()


# -- blocks -------------------------------------------------------------------------------

BLOCK_TYPES = {
    "heading": "text, level (1-3)",
    "markdown": "text (Markdown)",
    "metric": "label, table, aggregate (count|sum|avg|min|max), field, filters, format (number|money|percent)",
    "table": "table, columns (list), filters, order_by (list), limit, title",
    "chart": "table, x, y, kind (line|bar), group (a field: one series per value), aggregate (sum|avg|count), "
    "filters, title",
    "html": "source (raw Jinja/HTML)",
}


def _fmt(value, kind: str = "number") -> str:
    if value is None:
        return "—"
    if kind == "money":
        return money(value)
    if kind == "percent":
        return f"{float(value) * 100:.1f}%"
    return number(value)


def money(value) -> str:
    if value is None:
        return "—"
    return f"${float(value):,.2f}"


def number(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float | Decimal) and float(value) != int(float(value)):
        return f"{float(value):,.2f}"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


class Blocks:
    """The block macros: ``{{ blocks.metric(...) }}`` and friends. Each returns HTML."""

    def __init__(self, page: PageContext):
        self._page = page

    def _view(self, table: str, filters: dict | None = None) -> TableView:
        view = self._page.table(table)
        return view.filter(**filters) if filters else view

    def heading(self, text: str = "", level: int = 2, **_):
        from markupsafe import Markup
        from markupsafe import escape

        level = min(max(int(level or 2), 1), 3)
        return Markup(f"<h{level}>{escape(text)}</h{level}>")  # noqa: S704 — escaped above

    def markdown(self, text: str = "", **_):
        return markdown(text)

    def metric(  # noqa: PLR0913
        self,
        label: str = "",
        table: str = "",
        aggregate: str = "count",
        field: str = "",
        filters=None,
        format="number",  # noqa: A002 — the block field's name
        **_,
    ):
        from markupsafe import Markup
        from markupsafe import escape

        view = self._view(table, filters)
        value = (
            view.count() if aggregate == "count" else getattr(view, aggregate)(field)
        )
        return Markup(  # noqa: S704 — every value is escaped
            f'<div class="ergo-metric"><div class="ergo-metric-value">{escape(_fmt(value, format))}</div>'
            f'<div class="ergo-metric-label">{escape(label)}</div></div>'
        )

    def table(  # noqa: PLR0913
        self,
        table: str = "",
        columns=None,
        filters=None,
        order_by=None,
        limit: int = 50,
        title: str = "",
        **_,
    ):
        from markupsafe import Markup
        from markupsafe import escape

        view = self._view(table, filters)
        if order_by:
            view = view.order_by(*order_by)
        rows = view.limit(limit or 50).rows()
        cols = list(
            columns
            or [f for f in view.fields() if f not in ("created_at", "updated_at")]
        )
        head = "".join(f"<th>{escape(c)}</th>" for c in cols)
        body = "".join(
            "<tr>"
            + "".join(f"<td>{escape(_cell(r.get(c)))}</td>" for c in cols)
            + "</tr>"
            for r in rows
        )
        caption = f"<h3>{escape(title)}</h3>" if title else ""
        return Markup(  # noqa: S704 — every value is escaped
            f'{caption}<div class="ergo-table"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
        )

    def chart(  # noqa: PLR0913
        self,
        table: str = "",
        x: str = "",
        y: str = "",
        kind: str = "line",
        group: str = "",
        aggregate: str = "sum",
        filters=None,
        title: str = "",
        **_,
    ):
        from markupsafe import Markup
        from markupsafe import escape

        view = self._view(table, filters)
        spec = "count" if aggregate == "count" else f"{aggregate}:{y}"
        rows = view.group(*([x, group] if group else [x]), value=spec)
        labels = sorted({r[x] for r in rows}, key=lambda v: (v is None, v))
        series: dict[str, dict] = {}
        for r in rows:
            name = str(r[group]) if group else (y or "count")
            series.setdefault(name, {})[r[x]] = r["value"]
        data = {
            "type": "bar" if kind == "bar" else "line",
            "data": {
                "labels": [str(v) for v in labels],
                "datasets": [
                    {"label": name, "data": [values.get(v) for v in labels]}
                    for name, values in series.items()
                ],
            },
            "options": {"responsive": True, "maintainAspectRatio": False},
        }
        self._page.charts += 1
        chart_id = f"ergo-chart-{self._page.charts}"
        caption = f"<h3>{escape(title)}</h3>" if title else ""
        payload = json.dumps(data).replace("</", "<\\/")
        return Markup(  # noqa: S704 — the payload is JSON with </ escaped
            f'{caption}<div class="ergo-chart"><canvas id="{chart_id}"></canvas></div>'
            f"<script>new Chart(document.getElementById('{chart_id}'), {payload});</script>"
        )

    def html(self, source: str = "", **_):
        from markupsafe import Markup

        return Markup(self._page.render_string(source))  # noqa: S704 — rendered in the sandbox

    def render(self, block: dict):
        kind = str(block.get("type") or "")
        if kind not in BLOCK_TYPES:
            msg = f"Unknown block type {kind!r}; use one of {', '.join(BLOCK_TYPES)}"
            raise PageError(msg)
        return getattr(self, kind)(**{k: v for k, v in block.items() if k != "type"})


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return number(value)
    return str(value)


def markdown(text: str):
    from markdown_it import MarkdownIt
    from markupsafe import Markup

    # html=False: Markdown can't smuggle raw HTML in.
    rendered = (
        MarkdownIt("commonmark", {"html": False}).enable("table").render(text or "")
    )
    return Markup(rendered)  # noqa: S704


def blocks_source(title: str, blocks: list[dict]) -> str:
    """A .jhtml page made of block calls, one per block (what the pages plugin writes)."""
    lines = [
        "{# Written from blocks by ergo_page_write; edit the blocks or this file. #}"
    ]
    if title:
        lines.append("{{ blocks.heading(text=%s, level=1) }}" % json.dumps(title))  # noqa: UP031
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") not in BLOCK_TYPES:
            msg = f"Each block needs a type: one of {', '.join(BLOCK_TYPES)} (got {block!r})"
            raise PageError(msg)
        if block["type"] == "html":
            lines.append(str(block.get("source") or ""))
            continue
        lines.append("{{ blocks.render(%s) }}" % json.dumps(block))  # noqa: UP031
    return "\n\n".join(lines) + "\n"


# -- rendering ----------------------------------------------------------------------


LAYOUT = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<script src="{chart_js}"></script>
<style>
:root {{ --bg:#fff; --fg:#1d1d1f; --muted:#6b6b70; --line:#e4e4e7; --card:#f7f7f8; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#18181b; --fg:#f4f4f5; --muted:#a1a1aa; --line:#3f3f46; --card:#232327; }} }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.5 system-ui,sans-serif; margin:0; padding:20px 16px; }}
main {{ max-width:1100px; margin:0 auto; }}
h1 {{ font-size:24px; }} h2 {{ font-size:19px; margin-top:28px; }} h3 {{ font-size:15px; color:var(--muted); }}
.ergo-metric {{ display:inline-block; min-width:150px; margin:0 12px 12px 0; padding:12px 16px; background:var(--card);
  border:1px solid var(--line); border-radius:10px; vertical-align:top; }}
.ergo-metric-value {{ font-size:26px; font-weight:600; }}
.ergo-metric-label {{ color:var(--muted); font-size:13px; }}
.ergo-table {{ overflow-x:auto; margin-bottom:16px; }}
table {{ border-collapse:collapse; width:100%; font-size:14px; }}
th, td {{ text-align:left; padding:6px 10px; border-bottom:1px solid var(--line); white-space:nowrap; }}
th {{ color:var(--muted); font-weight:500; }}
.ergo-chart {{ position:relative; height:300px; margin-bottom:16px; }}
</style></head>
<body><main>
{body}
</main></body></html>
"""


class PageContext:
    """Everything one render can see."""

    def __init__(self, bot: Bot, user=None):
        self.bot = bot
        self.user = user
        self.charts = 0
        self.env = make_environment(bot)
        self.tables = {t.__name__.lower(): t for t in getattr(bot, "tables", [])}

    def table(self, name: str) -> TableView:
        model = self.tables.get(str(name).lower())
        if model is None:
            known = ", ".join(t.__name__ for t in self.tables.values()) or "none"
            msg = f"No table {name!r} (tables: {known})"
            raise PageError(msg)
        return TableView(model)

    def globals(self) -> dict:
        from django.conf import settings
        from django.utils import timezone

        now = timezone.localtime() if settings.USE_TZ else dt.datetime.now()  # noqa: DTZ005
        return {
            "table": self.table,
            "tables": [t.__name__ for t in self.tables.values()],
            "blocks": Blocks(self),
            "bot": {
                "name": self.bot.name,
                "description": getattr(self.bot.definition, "description", ""),
            },
            "user": {"username": self.user.get_username()}
            if self.user is not None
            else None,
            "now": now,
            "today": now.date(),
            "days_ago": lambda n: (now - dt.timedelta(days=int(n))).date(),
        }

    def render_string(self, source: str) -> str:
        return self.env.from_string(source, globals=self.globals()).render()


def make_environment(bot: Bot):
    from jinja2 import ChoiceLoader
    from jinja2 import FileSystemLoader
    from jinja2 import StrictUndefined
    from jinja2.sandbox import ImmutableSandboxedEnvironment

    root = getattr(bot.definition, "root_dir", None)
    loader = ChoiceLoader([FileSystemLoader(str(root))]) if root else None
    env = ImmutableSandboxedEnvironment(
        loader=loader, autoescape=True, undefined=StrictUndefined
    )
    env.filters.update(
        money=money,
        number=number,
        markdown=markdown,
        percent=lambda v: _fmt(v, "percent"),
    )
    return env


def render_page(bot: Bot, source: str, *, user=None, title: str = "") -> str:
    """Render ``.jhtml`` source to a full HTML page. Raises PageError with the reason."""
    from jinja2 import TemplateError

    page = PageContext(bot, user)
    try:
        body = page.render_string(source)
    except PageError:
        raise
    except TemplateError as exc:
        where = f" (line {exc.lineno})" if getattr(exc, "lineno", None) else ""
        msg = f"{type(exc).__name__}{where}: {exc}"
        raise PageError(msg) from exc
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        raise PageError(msg) from exc
    if "<html" in body[:500].lower():
        return body
    from markupsafe import escape

    return LAYOUT.format(title=escape(title or bot.name), chart_js=CHART_JS, body=body)


def error_page(message: str, title: str = "") -> str:
    from markupsafe import escape

    body = f"<h2>This page couldn't render</h2><pre style='white-space:pre-wrap'>{escape(message)}</pre>"
    return LAYOUT.format(
        title=escape(title or "Page error"), chart_js=CHART_JS, body=body
    )


def page_text(html: str, limit: int = 4000) -> str:
    """A rough text version of a rendered page, for the bot to check its work."""
    import re

    text = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<(br|/p|/div|/tr|/h\d|/li)\b[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text[:limit]


# -- pins ---------------------------------------------------------------------------

SERVED_SUFFIXES = {
    ".jhtml": "text/html",
    ".html": "text/html",
    ".htm": "text/html",
    ".mjs": "text/javascript",
    ".js": "text/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".csv": "text/csv",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def bot_file(bot: Bot, relative: str):
    """A file in the bot folder that may be served to the bot's users, or None.

    Only page and asset types (``SERVED_SUFFIXES``), never Python, YAML or
    dotfiles, and nothing outside the folder.
    """
    from pathlib import Path

    root = getattr(bot.definition, "root_dir", None)
    if root is None or not relative:
        return None
    parts = Path(relative).parts
    if (
        any(p.startswith(".") or p == ".." for p in parts)
        or Path(relative).is_absolute()
    ):
        return None
    path = (root / relative).resolve()
    if not path.is_relative_to(Path(root).resolve()) or not path.is_file():
        return None
    if path.suffix.lower() not in SERVED_SUFFIXES:
        return None
    return path


def session_pins(bot: Bot, session) -> list[dict]:
    """What's pinned in a chat: bot-folder files from ``chats.<name>.pins``, then pinned chat files."""
    chat = bot.chat_name(session)
    definition = bot.definition.chat(chat) if chat else None
    pins = [
        {
            "kind": "bot_file",
            "name": relative.rsplit("/", 1)[-1],
            "path": relative,
            "exists": bot_file(bot, relative) is not None,
        }
        for relative in (definition.pins if definition else [])
    ]
    pins.extend(
        {
            "kind": "file",
            "name": (row.metadata or {}).get("title") or row.filename,
            "id": str(row.id),
            "filename": row.filename,
        }
        for row in session.attachments.filter(metadata__pinned=True).order_by(
            "created_at"
        )
    )
    return pins


# -- viewing files ----------------------------------------------------------------------

VIEW_STYLE = (
    "<style>pre{white-space:pre-wrap;word-break:break-word;font:13px/1.5 ui-monospace,monospace;}"
    "img{max-width:100%}</style>"
)
MAX_VIEW_ROWS = 1000


def text_page(title: str, text: str, kind: str = "text") -> str:
    """A file shown as a page: ``markdown`` rendered (raw HTML stays escaped), ``csv`` as a
    table, ``json`` pretty-printed, anything else as preformatted text."""
    import csv
    import io

    from markupsafe import escape

    if kind == "markdown":
        body = str(markdown(text))
    elif kind == "csv":
        rows = list(csv.reader(io.StringIO(text)))[: MAX_VIEW_ROWS + 1]
        head = "".join(f"<th>{escape(c)}</th>" for c in (rows[0] if rows else []))
        cells = "".join(
            "<tr>" + "".join(f"<td>{escape(c)}</td>" for c in row) + "</tr>"
            for row in rows[1:]
        )
        body = f'<div class="ergo-table"><table><thead><tr>{head}</tr></thead><tbody>{cells}</tbody></table></div>'
    else:
        if kind == "json":
            import contextlib

            with contextlib.suppress(ValueError):
                text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        body = f"<pre>{escape(text)}</pre>"
    return LAYOUT.format(title=escape(title), chart_js=CHART_JS, body=VIEW_STYLE + body)


def view_kind(filename: str, media_type: str) -> str:  # noqa: PLR0911
    """How a file is shown in a viewer: page, image, pdf, media, markdown, csv, json, text,
    or "" (download only)."""
    name = filename.lower()
    if name.endswith(".jhtml"):
        return "page"
    if media_type == "text/html" or name.endswith((".html", ".htm")):
        return "html"
    if media_type in (
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/svg+xml",
    ):
        return "image"
    if media_type == "application/pdf":
        return "pdf"
    if media_type.startswith(("audio/", "video/")):
        return "media"
    if name.endswith((".md", ".markdown")) or media_type == "text/markdown":
        return "markdown"
    if name.endswith(".csv") or media_type == "text/csv":
        return "csv"
    if name.endswith(".json") or media_type == "application/json":
        return "json"
    from django_ergo.conversation.attachments import is_text

    return "text" if is_text(media_type) else ""
