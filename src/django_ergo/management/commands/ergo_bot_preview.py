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
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import models
from django.db import transaction


def sample_value(field: models.Field, index: int, *, empty: bool):
    if empty and field.null:
        return None
    if field.has_default():
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
        (models.CharField, f"sample {field.name} {index + 1}"),
        (models.TextField, f"sample {field.name} {index + 1}"),
    ]
    for kind, value in kinds:
        if isinstance(field, kind):
            limit = getattr(field, "max_length", None)
            return value[:limit] if isinstance(value, str) and limit else value
    if field.null:
        return None
    msg = f"no sample value for {field.name} ({field.get_internal_type()})"
    raise ValueError(msg)


def add_samples(table) -> int:
    """Four rows: all filled in, every optional field empty, and each half of them empty,
    so a page meets missing values alone and next to present ones."""
    fields = [
        f
        for f in table._meta.concrete_fields  # noqa: SLF001
        if not f.primary_key and f.name not in ("created_at", "updated_at")
    ]
    optional = [f.name for f in fields if f.null]
    patterns = [set(), set(optional), set(optional[0::2]), set(optional[1::2])]
    for index, empty in enumerate(patterns):
        table.objects.create(
            **{f.name: sample_value(f, index, empty=f.name in empty) for f in fields}
        )
    return len(patterns)


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
                for table in tables if samples else []:
                    if not table.objects.exists():
                        add_samples(table)
                        notes.append(
                            f"{table.__name__} was empty, so it has 4 sample rows (some optional fields empty)"
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
