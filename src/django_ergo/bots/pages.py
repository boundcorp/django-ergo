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

Rows give dates as ISO strings; the ``as_datetime``, ``seconds_until`` and
``duration`` filters turn them back into something to do math with
(``{{ row.resets_at | seconds_until | duration }}`` shows ``2h 15m``).

``blocks`` (see ``BLOCK_TYPES``) are the building blocks the pages plugin
writes pages from; hand-written pages can use them too.

Every rendered page also gets a small inline bridge script that defines
``window.ergo`` (see ``BRIDGE``). The page can't call the API itself: it runs
in a sandbox with no login. ``ergo.call(name, args)`` asks the app that shows
the page (Ergonaut's page viewer) to run one of the bot's page actions as the
viewer (see ``django_ergo.bots.page_actions``). The bridge also reports which
tables the page read, so the viewer re-renders the page when one changes:

    <button onclick="ergo.call('restock', {item: 'flour', qty: 2})">Restock</button>
    <script>ergo.on("table:Pantry", ev => ergo.reload())</script>
"""

from __future__ import annotations

import datetime as dt
import json
import re
from decimal import Decimal
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from collections.abc import Callable

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
    "form": "table, fields, values, submit, title",
    "table": "table, columns (list), filters, order_by (list), limit, title, edit, delete",
    "button": "label, action, args, confirm, ask",
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


def as_datetime(value) -> dt.datetime | None:
    """A row's ISO date string back to a datetime, so pages can do date math."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time())
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def seconds_until(value) -> float | None:
    """Seconds from now until a date (negative once it's past); None stays None."""
    when = as_datetime(value)
    if when is None:
        return None
    return (when - dt.datetime.now(tz=when.tzinfo)).total_seconds()


DURATION_UNITS = 2


def duration(seconds) -> str:
    """Seconds as the two largest units: ``2h 15m``, ``3d 4h``, ``45s``."""
    if seconds is None:
        return "—"
    total = int(abs(float(seconds)))
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if total >= size or (unit == "s" and not parts):
            parts.append(f"{total // size}{unit}")
            total %= size
        if len(parts) == DURATION_UNITS:
            break
    sign = "-" if float(seconds) < 0 else ""
    return sign + " ".join(parts)


def number(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float | Decimal) and float(value) != int(float(value)):
        return f"{float(value):,.2f}"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


TABLE_FORM_SCRIPT = r"""<script>
(function () {
  if (window.__ergoTableBlocks) return;
  window.__ergoTableBlocks = true;
  function errors(form, error) {
    form.querySelectorAll("[data-ergo-error]").forEach(function (node) { node.textContent = ""; });
    var fields = {};
    try { fields = (JSON.parse(error.message).field_errors || {}); } catch (_) {}
    Object.keys(fields).forEach(function (name) {
      var node = form.querySelector('[data-ergo-error="' + CSS.escape(name) + '"]');
      if (node) node.textContent = fields[name].join(" ");
    });
    var status = form.querySelector("[data-ergo-status]");
    if (status) status.textContent = Object.keys(fields).length ? "Please correct the highlighted fields." : error.message;
  }
  function values(form) {
    var data = {};
    new FormData(form).forEach(function (value, name) { data[name] = value; });
    form.querySelectorAll('input[type="checkbox"][name]').forEach(function (input) {
      data[input.name] = input.checked;
    });
    return data;
  }
  document.addEventListener("submit", async function (event) {
    var form = event.target.closest("form[data-ergo-action]");
    if (!form) return;
    event.preventDefault();
    var submit = form.querySelector('[type="submit"]');
    if (submit) submit.disabled = true;
    try {
      var args = {table: form.dataset.ergoTable, values: values(form)};
      if (form.dataset.ergoAction === "update") args.id = Number(form.dataset.ergoId);
      await ergo.call("ergo.table." + form.dataset.ergoAction, args);
    } catch (error) {
      errors(form, error);
    } finally {
      if (submit) submit.disabled = false;
    }
  });
  document.addEventListener("click", async function (event) {
    var button = event.target.closest("[data-ergo-delete]");
    if (!button) return;
    button.disabled = true;
    try {
      await ergo.call("ergo.table.delete", {table: button.dataset.ergoTable, id: Number(button.dataset.ergoId)});
    } catch (_) {
      // The bridge presents an action error as a toast; a delete has no field errors.
    } finally {
      button.disabled = false;
    }
  });
})();
</script>"""


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

    def _form_script(self) -> str:
        if self._page.form_script_added:
            return ""
        self._page.form_script_added = True
        return TABLE_FORM_SCRIPT

    def _editable_fields(self, table: str, fields=None):
        from django_ergo.bots.tables import writable_fields

        model = self._view(table)._model  # noqa: SLF001 — the view keeps the model private from templates
        if not getattr(model, "page_writes", True):
            msg = f"{model.__name__} does not allow page writes"
            raise PageError(msg)
        available = {field.name: field for field in writable_fields(model)}
        names = list(fields) if fields is not None else list(available)
        unknown = set(names) - set(available)
        if unknown:
            msg = f"{model.__name__} has no writable field(s) {', '.join(sorted(unknown))}"
            raise PageError(msg)
        return model, [available[name] for name in names]

    @staticmethod
    def _input(field, value) -> str:
        from markupsafe import escape

        name = escape(field.name)
        label = escape(field.verbose_name)
        required = " required" if not field.blank and not field.null else ""
        value = _plain(value)
        if field.choices:
            options = [
                f'<option value="{escape(key)}"{" selected" if str(key) == str(value) else ""}>{escape(text)}</option>'
                for key, text in field.flatchoices
            ]
            return f'<label>{label}<select name="{name}"{required}>{"".join(options)}</select></label>'
        kind = field.get_internal_type()
        if kind == "BooleanField":
            return (
                f'<label><input type="checkbox" name="{name}"{" checked" if value else ""}> '
                f"{label}</label>"
            )
        input_type = {
            "DateField": "date",
            "DateTimeField": "datetime-local",
            "TimeField": "time",
            "EmailField": "email",
            "URLField": "url",
            "IntegerField": "number",
            "BigIntegerField": "number",
            "FloatField": "number",
            "DecimalField": "number",
        }.get(kind, "text")
        step = ' step="1"' if input_type == "number" and "Integer" in kind else ""
        if input_type == "number" and not step:
            step = ' step="any"'
        if kind == "TextField":
            return f'<label>{label}<textarea name="{name}"{required}>{escape(value or "")}</textarea></label>'
        return f'<label>{label}<input type="{input_type}" name="{name}" value="{escape(value or "")}"{step}{required}></label>'

    def _form(  # noqa: PLR0913
        self,
        table: str,
        fields,
        values: dict,
        submit: str,
        *,
        action: str,
        row_id: int | None = None,
    ) -> str:
        from markupsafe import escape

        controls = "".join(
            self._input(
                field,
                values.get(
                    field.name, field.get_default() if field.has_default() else ""
                ),
            )
            + f'<span class="ergo-field-error" data-ergo-error="{escape(field.name)}"></span>'
            for field in fields
        )
        id_attr = f' data-ergo-id="{row_id}"' if row_id is not None else ""
        return (
            f'<form class="ergo-form" data-ergo-action="{action}" data-ergo-table="{escape(table)}"{id_attr}>'
            f'{controls}<p class="ergo-form-error" role="alert" data-ergo-status></p>'
            f'<button type="submit">{escape(submit)}</button></form>'
        )

    def form(
        self,
        table: str = "",
        fields=None,
        values=None,
        submit: str = "Add",
        title: str = "",
        **_,
    ):
        from markupsafe import Markup
        from markupsafe import escape

        if values is not None and not isinstance(values, dict):
            msg = "form values must be an object"
            raise PageError(msg)
        _, editable = self._editable_fields(table, fields)
        caption = f"<h3>{escape(title)}</h3>" if title else ""
        return Markup(  # noqa: S704 — form labels and values are escaped
            f"{caption}{self._form_script()}{self._form(table, editable, values or {}, submit, action='add')}"
        )

    def table(  # noqa: PLR0913
        self,
        table: str = "",
        columns=None,
        filters=None,
        order_by=None,
        limit: int = 50,
        title: str = "",
        edit: bool = False,
        delete: bool = False,
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
            or [
                field
                for field in view.fields()
                if field not in ("created_at", "updated_at")
            ]
        )
        can_write = (edit or delete) and getattr(view._model, "page_writes", True)  # noqa: SLF001
        editable = self._editable_fields(table)[1] if can_write and edit else []
        head = "".join(f"<th>{escape(column)}</th>" for column in cols)
        if can_write:
            head += "<th>Actions</th>"
        body = ""
        for row in rows:
            cells = "".join(
                f"<td>{escape(_cell(row.get(column)))}</td>" for column in cols
            )
            if can_write:
                actions = ""
                if edit:
                    actions += (
                        "<details><summary>Edit</summary>"
                        + self._form(
                            table,
                            editable,
                            row,
                            "Save",
                            action="update",
                            row_id=row["id"],
                        )
                        + "</details>"
                    )
                if delete:
                    actions += (
                        f'<button type="button" data-ergo-delete data-ergo-table="{escape(table)}" '
                        f'data-ergo-id="{row["id"]}">Delete</button>'
                    )
                cells += f"<td>{actions}</td>"
            body += f"<tr>{cells}</tr>"
        caption = f"<h3>{escape(title)}</h3>" if title else ""
        script = self._form_script() if can_write else ""
        return Markup(  # noqa: S704 — every value is escaped
            f'{caption}{script}<div class="ergo-table"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
        )

    def button(
        self,
        label: str = "",
        action: str = "",
        args=None,
        confirm: str = "",
        ask: str = "",
        **_,
    ):
        from markupsafe import Markup
        from markupsafe import escape

        if ask:
            action = "ergo.ask"
            args = {**(args or {}), "text": ask}
        if not action:
            msg = "button needs an action or ask"
            raise PageError(msg)
        if args is not None and not isinstance(args, dict):
            msg = "button args must be an object"
            raise PageError(msg)
        self._page.controls += 1
        button_id = f"ergo-button-{self._page.controls}"
        payload = json.dumps(args or {}).replace("</", "<\\/")
        confirmation = json.dumps(str(confirm)).replace("</", "<\\/")
        return Markup(  # noqa: S704 — text is escaped, arguments are JSON
            f'<button type="button" id="{button_id}">{escape(label)}</button>'
            f"<script>(function(){{var button=document.getElementById({json.dumps(button_id)});"
            f"button.addEventListener('click',async function(){{if({confirmation}&&!window.confirm({confirmation}))return;"
            "button.disabled=true;try{await ergo.call("
            f"{json.dumps(str(action))},{payload});}}catch(_){{}}finally{{button.disabled=false;}}}});}})();</script>"
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
<script>/* ?theme=dark|light from the app that shows this page (Ergonaut's theme toggle) */
document.documentElement.dataset.theme = new URLSearchParams(location.search).get("theme") || "";</script>
<script src="{chart_js}"></script>
<style>
:root {{ --bg:#fff; --fg:#1d1d1f; --muted:#6b6b70; --line:#e4e4e7; --card:#f7f7f8; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme=light]) {{ --bg:#18181b; --fg:#f4f4f5; --muted:#a1a1aa; --line:#3f3f46; --card:#232327; }} }}
:root[data-theme=dark] {{ --bg:#1b2534; --fg:#f5f7fb; --muted:#b9c5d8; --line:#3b4b62; --card:#253246; color-scheme:dark; }}
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
.ergo-form {{ display:grid; gap:8px; max-width:480px; margin:0 0 16px; }}
.ergo-form label {{ display:grid; gap:3px; font-size:13px; color:var(--muted); }}
.ergo-form input, .ergo-form select, .ergo-form textarea {{ box-sizing:border-box; width:100%; padding:6px 8px; color:var(--fg); background:var(--bg); border:1px solid var(--line); border-radius:5px; font:inherit; }}
.ergo-form input[type=checkbox] {{ width:auto; }}
.ergo-form button, .ergo-table button, main > button {{ width:max-content; padding:6px 10px; color:var(--fg); background:var(--card); border:1px solid var(--line); border-radius:5px; font:inherit; cursor:pointer; }}
.ergo-field-error, .ergo-form-error {{ color:#c2410c; font-size:13px; }}
details .ergo-form {{ margin-top:8px; }}
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
        self.controls = 0
        self.form_script_added = False
        self.tables_read: dict[str, None] = {}
        # Every table the page read, in order (live refresh watches these).
        self.env = make_environment(bot)
        self.tables = {t.__name__.lower(): t for t in getattr(bot, "tables", [])}

    def table(self, name: str) -> TableView:
        model = self.tables.get(str(name).lower())
        if model is None:
            known = ", ".join(t.__name__ for t in self.tables.values()) or "none"
            msg = f"No table {name!r} (tables: {known})"
            raise PageError(msg)
        self.tables_read[model.__name__] = None
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
        as_datetime=as_datetime,
        seconds_until=seconds_until,
        duration=duration,
    )
    return env


def render_page(  # noqa: PLR0913
    bot: Bot,
    source: str,
    *,
    user=None,
    title: str = "",
    page_path: str = "",
    asset_url: Callable[[str], str] | None = None,
) -> str:
    """Render ``.jhtml`` source to a full HTML page. Raises PageError with the reason.

    The page gets the ``window.ergo`` bridge (see ``BRIDGE``) with the tables it read.
    A page served from a sandbox can't send its login with asset requests, so for a
    bot-folder page (``page_path``, relative to the bot folder) ``asset_url`` maps a
    bot-relative path to the URL to load it from, and the page's relative ``src`` and
    ``href`` references to folder files (scripts, styles, images, ``url()`` in styles)
    are rewritten with it.
    """
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
    if "<html" not in body[:500].lower():
        from markupsafe import escape

        body = LAYOUT.format(
            title=escape(title or bot.name), chart_js=CHART_JS, body=body
        )
    if asset_url is not None:
        body = sign_assets(bot, body, page_path, asset_url)
    return inject_bridge(body, list(page.tables_read))


# -- the bridge ---------------------------------------------------------------------------

# Defines window.ergo in every rendered page. It never makes a request: the page runs
# sandboxed (no login, no app API), so it talks to the app that shows it by postMessage.
#   page -> viewer: ergo:call {id, name, args}, ergo:ready {tables, listening}, ergo:reload,
#                   ergo:focus {focused}, ergo:scroll {y}
#   viewer -> page: ergo:result {id, ok, result | error}, ergo:changed {table},
#                   ergo:getscroll, ergo:restore {y}
BRIDGE = r"""(function () {
  var tables = __TABLES__;
  var parent = window.parent !== window ? window.parent : null;
  var pending = {};
  var handlers = {};
  var count = 0;
  var readyTimer = 0;
  var restoreY = null;
  function post(message) { if (parent) parent.postMessage(message, "*"); }
  function canonical(name) {
    var wanted = String(name).toLowerCase();
    for (var i = 0; i < tables.length; i++) if (tables[i].toLowerCase() === wanted) return tables[i];
    return String(name);
  }
  function listening() {
    return Object.keys(handlers)
      .filter(function (key) { return key.indexOf("table:") === 0 && handlers[key].length; })
      .map(function (key) { return key.slice(6); });
  }
  function ready() {
    readyTimer = 0;
    post({type: "ergo:ready", tables: tables, listening: listening()});
  }
  function field(el) {
    return !!el && (/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || el.isContentEditable);
  }
  var ergo = {
    tables: tables,
    call: function (name, args) {
      return new Promise(function (resolve, reject) {
        if (!parent) { reject(new Error("Open this page in Ergonaut to use its buttons")); return; }
        var id = "c" + (++count);
        pending[id] = {resolve: resolve, reject: reject};
        post({type: "ergo:call", id: id, name: String(name), args: args || {}});
      });
    },
    ask: function (text, options) {
      options = options || {};
      return ergo.call("ergo.ask", {text: String(text), chat: options.chat || "main"});
    },
    on: function (event, handler) {
      var key = String(event).indexOf("table:") === 0 ? "table:" + canonical(String(event).slice(6)) : String(event);
      (handlers[key] = handlers[key] || []).push(handler);
      if (key.indexOf("table:") === 0 && !readyTimer) readyTimer = setTimeout(ready, 0);
      return function off() {
        handlers[key] = (handlers[key] || []).filter(function (h) { return h !== handler; });
        if (key.indexOf("table:") === 0 && !readyTimer) readyTimer = setTimeout(ready, 0);
      };
    },
    reload: function () { post({type: "ergo:reload"}); }
  };
  window.ergo = ergo;
  window.addEventListener("message", function (event) {
    var data = event.data;
    if (!parent || event.source !== parent || !data || typeof data.type !== "string") return;
    if (data.type === "ergo:result") {
      var waiting = pending[data.id];
      delete pending[data.id];
      if (!waiting) return;
      if (data.ok) waiting.resolve(data.result); else waiting.reject(new Error(data.error || "The action failed"));
    } else if (data.type === "ergo:changed") {
      (handlers["table:" + canonical(data.table)] || []).slice().forEach(function (handler) {
        try { handler({type: "changed", table: data.table}); } catch (error) { console.error(error); }
      });
    } else if (data.type === "ergo:getscroll") {
      post({type: "ergo:scroll", y: window.scrollY});
    } else if (data.type === "ergo:restore") {
      restoreY = Number(data.y) || 0;
      window.scrollTo(0, restoreY);
    }
  });
  window.addEventListener("load", function () { if (restoreY !== null) window.scrollTo(0, restoreY); });
  document.addEventListener("focusin", function (event) { if (field(event.target)) post({type: "ergo:focus", focused: true}); });
  document.addEventListener("focusout", function (event) { if (field(event.target)) post({type: "ergo:focus", focused: false}); });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", ready); else ready();
})();"""


def bridge_script(tables: list[str]) -> str:
    """The ``<script>`` that defines ``window.ergo``, told which tables the page read."""
    data = json.dumps(tables).replace("</", "<\\/").replace("<!--", "<\\!--")
    return f"<script>{BRIDGE.replace('__TABLES__', data)}</script>"


def inject_bridge(html: str, tables: list[str]) -> str:
    """Put the bridge first in the page's head (so page scripts can use ``ergo``), whether
    or not the page has its own ``<head>`` or ``<html>``."""
    import re

    script = bridge_script(tables)
    for pattern in (r"<head\b[^>]*>", r"<html\b[^>]*>"):
        found = re.search(pattern, html, flags=re.IGNORECASE)
        if found:
            return html[: found.end()] + script + html[found.end() :]
    return script + html


# -- assets of a sandboxed page -----------------------------------------------------------

ASSET_TAGS = re.compile(
    r"<(?:script|link|img|source|video|audio)\b[^>]*>", re.IGNORECASE
)
ASSET_ATTR = re.compile(
    r"""(?P<head>\b(?:src|href|poster)\s*=\s*)(?P<quote>["'])(?P<url>[^"']*)(?P=quote)""",
    re.IGNORECASE,
)
STYLE_BLOCK = re.compile(
    r"(?P<open><style\b[^>]*>)(?P<css>.*?)(?P<close></style>)",
    re.IGNORECASE | re.DOTALL,
)
STYLE_ATTR = re.compile(
    r"""(?P<head>\bstyle\s*=\s*)(?P<quote>["'])(?P<css>.*?)(?P=quote)""",
    re.IGNORECASE | re.DOTALL,
)
CSS_URL = re.compile(r"""url\(\s*(?P<quote>["']?)(?P<url>[^"')]*)(?P=quote)\s*\)""")


def folder_asset(bot: Bot, page_path: str, reference: str) -> str | None:
    """The bot-relative path a page's relative reference points at, if it's a file
    ``bot_file`` serves (and not a page: pages render with data, so they're never linked
    with a token)."""
    import posixpath
    from urllib.parse import urlsplit

    parts = urlsplit(reference.strip())
    if parts.scheme or parts.netloc or not parts.path or parts.path.startswith("/"):
        return None
    if parts.query:
        return None
    relative = posixpath.normpath(
        posixpath.join(posixpath.dirname(page_path), parts.path)
    )
    if relative.startswith("..") or bot_file(bot, relative) is None:
        return None
    if relative.lower().endswith(PAGE_SUFFIXES):
        return None
    return relative


def sign_assets(
    bot: Bot, html: str, page_path: str, asset_url: Callable[[str], str]
) -> str:
    """Point the page's relative asset references (``src`` and ``href`` of scripts, links,
    images and media; ``url()`` in style blocks and ``style`` attributes) at
    ``asset_url(path)``. Links (``<a href>``), other hosts and missing files are left alone."""
    from urllib.parse import urlsplit

    def target(reference: str) -> str | None:
        relative = folder_asset(bot, page_path, reference)
        if relative is None:
            return None
        fragment = urlsplit(reference.strip()).fragment
        return asset_url(relative) + (f"#{fragment}" if fragment else "")

    def attribute(found: re.Match) -> str:
        new = target(found["url"])
        return (
            found[0]
            if new is None
            else f"{found['head']}{found['quote']}{new}{found['quote']}"
        )

    def css(text: str) -> str:
        return CSS_URL.sub(
            lambda found: found[0]
            if (new := target(found["url"])) is None
            else f"url({found['quote']}{new}{found['quote']})",
            text,
        )

    html = ASSET_TAGS.sub(lambda tag: ASSET_ATTR.sub(attribute, tag[0]), html)
    html = STYLE_BLOCK.sub(lambda m: m["open"] + css(m["css"]) + m["close"], html)
    return STYLE_ATTR.sub(
        lambda m: f"{m['head']}{m['quote']}{css(m['css'])}{m['quote']}", html
    )


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
            **pin_label(definition, relative),
            "path": relative,
            "exists": bot_file(bot, relative) is not None,
        }
        for relative in (definition.pins if definition else [])
    ]
    pins.extend(
        {
            "kind": "file",
            "name": (row.metadata or {}).get("title") or row.filename,
            "icon": (row.metadata or {}).get("icon") or "",
            "id": str(row.id),
            "filename": row.filename,
        }
        for row in session.attachments.filter(metadata__pinned=True).order_by(
            "created_at"
        )
    )
    return pins


def pin_label(chat, relative: str) -> dict:
    """A bot-folder pin's ``name`` (its title, or the file name) and ``icon`` ("" = by file type)."""
    label = chat.pin_labels.get(relative, {}) if chat else {}
    return {
        "name": label.get("title") or relative.rsplit("/", 1)[-1],
        "icon": label.get("icon", ""),
    }


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
