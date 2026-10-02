"""Bot tables: real Django models declared in a bot's own Python files.

    # bot.yaml
    tables: [tables.py]

    # tables.py
    from django.db import models
    from django_ergo.bots import BotTable

    class House(BotTable):
        \"\"\"Houses we've looked at for the property search.\"\"\"

        address = models.CharField(max_length=200)
        price = models.IntegerField(null=True, blank=True)
        notes = models.TextField(blank=True)

Each bot is its own Django app (label ``ergo_bot_<name>``, tables
``ergo_bot_<name>_<model>``), so the ORM, the admin and Jinja pages all work
on its tables. Everyone shares the rows.

Schema changes are migrations in the bot folder's ``migrations/``, written by
``python -m django ergo_bot_makemigrations <bot folder>`` (the
bot_management plugin runs it, so a proposal that changes a table also
carries its migration) and applied by ``ergo_bot_migrate`` (Ergonaut runs it
at start and after pulling the bot repo). Only migrations in the folder are
applied, so every schema change goes through the bot repo, and data
migrations have a place to live.

Every table gets tools through the ``tables`` skill: query, add, update and
delete (delete waits for approval). A model's docstring describes it.
"""

from __future__ import annotations

import contextvars
import re
import sys
import types
from typing import TYPE_CHECKING
from typing import Any

from django.apps import AppConfig
from django.apps import apps as global_apps
from django.db import models
from django.db.models.base import ModelBase

if TYPE_CHECKING:
    from pathlib import Path

    from django_ergo.bots.tools import BotTool

_LOADING: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ergo_bot_table_app", default=""
)
MAX_ROWS = 200
# Each table file's module, so a bot's other code can use it without importing it again.
MODULES: dict[Path, types.ModuleType] = {}


def app_label_for(bot_name: str) -> str:
    return "ergo_bot_" + re.sub(r"\W", "_", bot_name).lower()


class BotTableBase(ModelBase):
    """Gives each bot table its bot's app label and a namespaced table name."""

    def __new__(mcs, name, bases, attrs, **kwargs):
        meta = attrs.get("Meta")
        abstract = bool(meta and getattr(meta, "abstract", False))
        if not abstract and attrs.get("__module__") != __name__:
            label = _LOADING.get()
            if not label:
                msg = f"{name}: bot tables are loaded from a bot's tables: files"
                raise RuntimeError(msg)
            meta = meta or type("Meta", (), {})
            meta.app_label = label
            if not hasattr(meta, "db_table"):
                meta.db_table = f"{label}_{name.lower()}"
            attrs["Meta"] = meta
        return super().__new__(mcs, name, bases, attrs, **kwargs)


class BotTable(models.Model, metaclass=BotTableBase):
    """Base class for a bot's tables. Subclass it in a file listed under ``tables:``."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class BotAppConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"


def package_name(bot_name: str) -> str:
    return "ergo_bot_app_" + re.sub(r"\W", "_", bot_name).lower()


def register_app(bot_name: str, root_dir: Path) -> AppConfig:
    """Make the bot folder a Django app: its ``migrations/`` holds the bot's migrations."""
    label = app_label_for(bot_name)
    name = package_name(bot_name)
    module = sys.modules.get(name)
    if module is None or list(getattr(module, "__path__", [])) != [str(root_dir)]:
        module = types.ModuleType(name)
        module.__path__ = [str(root_dir)]
        module.__file__ = str(root_dir / "__init__.py")
        sys.modules[name] = module
        for stale in [m for m in sys.modules if m.startswith(f"{name}.")]:
            sys.modules.pop(stale)
    config = global_apps.app_configs.get(label)
    if config is None or config.name != name or config.module is not module:
        config = BotAppConfig(name, module)
        config.label = label
        config.verbose_name = f"Bot {bot_name}"
        config.apps = global_apps
        config.models = global_apps.all_models[label]
        global_apps.app_configs[label] = config
        global_apps.clear_cache()
    return config


def load_tables(
    bot_name: str, root_dir: Path, files: list[Path]
) -> list[type[BotTable]]:
    """Import the bot's table files and return its tables."""
    from django_ergo.bots.tools import load_tool_module

    if not files:
        return []
    register_app(bot_name, root_dir)
    label = app_label_for(bot_name)
    # Loading again (the bot's files changed) replaces its models rather than piling up.
    global_apps.all_models[label].clear()
    token = _LOADING.set(label)
    try:
        for path in files:
            MODULES[path] = load_tool_module(path, bot_name).module
    finally:
        _LOADING.reset(token)
    global_apps.clear_cache()
    return [
        m for m in global_apps.all_models[label].values() if issubclass(m, BotTable)
    ]


# -- tools ------------------------------------------------------------------------------


def describe(table: type[BotTable]) -> str:
    lines = [
        f"{table.__name__}: {(table.__doc__ or '').strip().splitlines()[0] if table.__doc__ else ''}".rstrip(
            ": "
        )
    ]
    for field in table._meta.get_fields():
        if field.auto_created and not field.concrete:
            continue
        kind = field.get_internal_type()
        extra = []
        if getattr(field, "null", False):
            extra.append("optional")
        if field.choices:
            extra.append("one of " + ", ".join(str(c[0]) for c in field.choices))
        lines.append(
            f"  - {field.name} ({kind}{', ' + ', '.join(extra) if extra else ''})"
        )
    return "\n".join(lines)


def _row(obj: models.Model) -> dict:
    from django.forms.models import model_to_dict

    data = model_to_dict(obj)
    data["id"] = obj.pk
    for name in ("created_at", "updated_at"):
        value = getattr(obj, name, None)
        if value is not None:
            data[name] = value.isoformat(timespec="seconds")
    return {
        k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in data.items()
    }


def table_tools(tables: list[type[BotTable]]) -> list[BotTool]:  # noqa: C901
    """Query, add, update and delete tools over ``tables``."""
    from django_ergo.bots.tools import bot_tool

    by_name = {t.__name__.lower(): t for t in tables}
    names = ", ".join(t.__name__ for t in tables)

    def lookup(name: str) -> type[BotTable]:
        found = by_name.get(str(name).lower())
        if found is None:
            msg = f"No table {name!r}. Tables: {names}"
            raise ValueError(msg)
        return found

    def clean(model: type[BotTable], values: dict) -> dict:
        allowed = {f.name for f in model._meta.concrete_fields} - {
            "id",
            "created_at",
            "updated_at",
        }
        unknown = set(values) - allowed
        if unknown:
            msg = f"{model.__name__} has no field(s) {', '.join(sorted(unknown))}"
            raise ValueError(msg)
        return values

    @bot_tool(
        name="ergo_table_query",
        description=f"Find rows in a table ({names}). Filters use Django lookups, e.g. "
        '{"price__lte": 500000, "address__icontains": "oak"}.',
        parameters={
            "table": {"type": "string"},
            "filters": {
                "type": "object",
                "description": "Field lookups to match (all must match)",
            },
            "order_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": 'e.g. ["-price"]',
            },
            "limit": {
                "type": "integer",
                "description": f"Default 20, at most {MAX_ROWS}",
            },
            "count_only": {"type": "boolean"},
        },
        required=["table"],
    )
    def query(
        table: str,
        filters: dict | None = None,
        order_by: list | None = None,
        limit: int = 20,
        count_only: bool = False,
    ) -> Any:
        model = lookup(table)
        qs = model.objects.filter(**(filters or {}))
        if count_only:
            return {"count": qs.count()}
        if order_by:
            qs = qs.order_by(*order_by)
        rows = [_row(obj) for obj in qs[: max(1, min(int(limit or 20), MAX_ROWS))]]
        return {"count": qs.count(), "rows": rows}

    @bot_tool(
        name="ergo_table_add",
        description=f"Add a row to a table ({names}).",
        parameters={"table": {"type": "string"}, "values": {"type": "object"}},
        required=["table", "values"],
    )
    def add(table: str, values: dict) -> dict:
        model = lookup(table)
        obj = model(**clean(model, values))
        obj.full_clean(exclude=["created_at", "updated_at"])
        obj.save()
        return _row(obj)

    @bot_tool(
        name="ergo_table_update",
        description=f"Change fields of one row of a table ({names}) by id.",
        parameters={
            "table": {"type": "string"},
            "id": {"type": "integer"},
            "values": {"type": "object"},
        },
        required=["table", "id", "values"],
    )
    def update(table: str, id: int, values: dict) -> dict:  # noqa: A002
        model = lookup(table)
        obj = model.objects.get(pk=id)
        for key, value in clean(model, values).items():
            setattr(obj, key, value)
        obj.full_clean(exclude=["created_at", "updated_at"])
        obj.save()
        return _row(obj)

    @bot_tool(
        name="ergo_table_delete",
        description=f"Delete one row of a table ({names}) by id.",
        parameters={"table": {"type": "string"}, "id": {"type": "integer"}},
        required=["table", "id"],
        requires_approval=True,
    )
    def delete(table: str, id: int) -> str:  # noqa: A002
        model = lookup(table)
        model.objects.filter(pk=id).delete()
        return f"Deleted {model.__name__} {id}"

    return [fn.__bot_tool__ for fn in (query, add, update, delete)]
