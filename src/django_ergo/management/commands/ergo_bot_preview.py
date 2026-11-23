"""Render a bot folder's .jhtml page, e.g. from a draft, without changing anything.

    python -m django ergo_bot_preview path/to/bot pages/dashboard.jhtml

Everything happens in one database transaction that is rolled back: the
bot's own migrations are applied (so a draft's new or changed tables exist),
empty tables get sample rows (one filled in, others with some or all
optional fields empty, so pages are tried against missing values too), and the page renders.
Prints JSON: ``{"ok": true, "preview": "<page text>", "notes": [...]}`` or
``{"ok": false, "error": "..."}``. Run it in its own process: it loads the
folder's tables under the bot's app label, which would replace a running
bot's models.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import IntegrityError
from django.db import models
from django.db import transaction


def sample_value(field: models.Field, index: int, *, empty: bool):
    if empty and field.null:
        return None
    # A unique field needs a different value per row, so its default won't do.
    if field.has_default() and not field.unique:
        default = field.get_default()
        if default is not None and not isinstance(
            field, models.DateField | models.DateTimeField
        ):
            return default
    if field.choices:
        return field.choices[index % len(field.choices)][0]
    now = dt.datetime.now(tz=dt.UTC)
    kinds = [
        (models.BooleanField, True),
        (models.DateTimeField, now - dt.timedelta(days=index)),
        (models.DateField, (now - dt.timedelta(days=index)).date()),
        (models.DecimalField, Decimal("12.34") + index),
        (models.FloatField, 1.5 + index),
        (models.IntegerField, 10 + index),  # includes the other integer fields
        (models.JSONField, {}),
        (models.UUIDField, uuid.UUID(int=index + 1)),
        (models.CharField, f"sample {field.name} {index + 1}"),
        (models.TextField, f"sample {field.name} {index + 1}"),
    ]
    for kind, value in kinds:
        if isinstance(field, kind):
            limit = getattr(field, "max_length", None)
            if isinstance(value, str) and limit and len(value) > limit:
                # Short fields keep the row number, so unique ones stay distinct.
                return f"{index + 1}{field.name}"[:limit]
            return value
    if field.null:
        return None
    msg = f"no sample value for {field.name} ({field.get_internal_type()})"
    raise ValueError(msg)


def seed_order(tables) -> list:
    """Tables with the ones they point to first; self-references and cycles are tolerated
    (what can't be ordered keeps its place at the end)."""
    mine = set(tables)
    deps = {
        t: {
            f.related_model
            for f in t._meta.concrete_fields  # noqa: SLF001
            if f.is_relation and f.related_model in mine and f.related_model is not t
        }
        for t in tables
    }
    ordered: list = []
    pending = list(tables)
    while pending:
        ready = [t for t in pending if deps[t] <= set(ordered)]
        if not ready:  # a cycle: take the first and carry on
            ready = [pending[0]]
        ordered.extend(ready)
        pending = [t for t in pending if t not in ready]
    return ordered


def add_samples(table) -> tuple[int, str]:
    """Four rows: all filled in, every optional field empty, and each half of them empty,
    so a page meets missing values alone and next to present ones.

    Foreign keys point at rows the target table already has (sample or real). Returns
    the number of rows added and, when none could be, why not."""
    fields = [
        f
        for f in table._meta.concrete_fields  # noqa: SLF001
        if not f.primary_key and f.name not in ("created_at", "updated_at")
    ]
    optional = [f.name for f in fields if f.null]
    patterns = [set(), set(optional), set(optional[0::2]), set(optional[1::2])]
    added = 0
    for index, empty in enumerate(patterns):
        values = {}
        usable = True
        for f in fields:
            if not (f.is_relation and (f.many_to_one or f.one_to_one)):
                values[f.name] = sample_value(f, index, empty=f.name in empty)
                continue
            if f.null and f.name in empty:
                values[f.name] = None
                continue
            target = f.related_model
            rows = list(target._default_manager.order_by("pk"))  # noqa: SLF001
            # A unique link (one-to-one) needs a different target per row.
            n = index if f.unique else (index % len(rows) if rows else 0)
            if n < len(rows):
                values[f.name] = rows[n]
            elif f.null:
                values[f.name] = None
            elif not rows and not added:
                return (
                    0,
                    f"{f.name} needs a {target.__name__} and there are none to point at",
                )
            else:
                usable = False  # unique and the targets ran out
                break
        if not usable:
            continue
        try:
            with transaction.atomic():
                table.objects.create(**values)
        except IntegrityError:
            continue  # clashes with an earlier sample under a unique constraint
        added += 1
    return added, ""


class Command(BaseCommand):
    help = "Render a .jhtml page from a bot folder (e.g. a draft) and print its text as JSON; changes nothing."

    def add_arguments(self, parser):
        parser.add_argument("folder", help="The bot folder (with bot.yaml)")
        parser.add_argument("page", help="The page, relative to the bot folder")
        parser.add_argument(
            "--no-samples",
            action="store_true",
            help="Don't add sample rows to empty tables",
        )

    def handle(self, *args, folder, page, no_samples=False, **options):
        result = self.preview(
            Path(folder).expanduser().resolve(), page, samples=not no_samples
        )
        self.stdout.write(json.dumps(result))

    def preview(self, folder: Path, page: str, *, samples: bool) -> dict:
        from django_ergo.bots.definition import BotDefinition
        from django_ergo.bots.pages import PageError
        from django_ergo.bots.pages import page_text
        from django_ergo.bots.pages import render_page
        from django_ergo.bots.tables import app_label_for
        from django_ergo.bots.tables import load_tables

        path = (folder / page).resolve()
        if (
            not path.is_relative_to(folder)
            or not path.is_file()
            or path.suffix != ".jhtml"
        ):
            return {"ok": False, "error": f"No .jhtml page {page} in {folder.name}"}
        notes = []
        try:
            definition = BotDefinition.load(folder)
            tables = (
                load_tables(
                    definition.name, definition.root_dir, definition.table_files
                )
                if definition.table_files
                else []
            )
        except Exception as exc:  # noqa: BLE001 — reported to the bot
            return {
                "ok": False,
                "error": f"The bot didn't load: {type(exc).__name__}: {exc}",
            }
        bot = SimpleNamespace(
            name=definition.name, definition=definition, tables=tables
        )
        with transaction.atomic():
            try:
                if tables and (folder / "migrations").is_dir():
                    call_command(
                        "migrate",
                        app_label_for(definition.name),
                        verbosity=0,
                        interactive=False,
                    )
                for table in seed_order(tables) if samples else []:
                    if table.objects.exists():
                        continue
                    try:
                        added, why = add_samples(table)
                    except Exception as exc:  # noqa: BLE001 — one table's gap must not stop the render
                        added, why = 0, f"{type(exc).__name__}: {exc}"
                    if why:
                        notes.append(
                            f"{table.__name__} was empty and got no sample rows: {why}"
                        )
                    else:
                        notes.append(
                            f"{table.__name__} was empty, so it has {added} sample rows (some optional fields empty)"
                        )
                html = render_page(bot, path.read_text(), title=path.stem)
                result = {"ok": True, "preview": page_text(html, 3000), "notes": notes}
            except PageError as exc:
                result = {"ok": False, "error": str(exc), "notes": notes}
            except Exception as exc:  # noqa: BLE001 — reported to the bot
                result = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "notes": notes,
                }
            transaction.set_rollback(True)
        result["notes"] = [*result.get("notes", []), "Nothing was saved."]
        return result
