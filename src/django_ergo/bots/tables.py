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

Pages that read a table re-render when it changes (live refresh). Saving or
deleting a row announces that through the ``table_changed`` signal once its
transaction commits. Bulk writes (``.update()``, ``bulk_create``, ``.delete()``
on a queryset, raw SQL) skip the model signals, so call ``touch()`` after
them: ``ctx.table("House").touch()``.
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
from django.db import transaction
from django.db.models.base import ModelBase
from django.db.models.signals import post_delete
from django.db.models.signals import post_save
from django.dispatch import Signal

if TYPE_CHECKING:
    from pathlib import Path

    from django_ergo.bots.tools import BotTool

_LOADING: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ergo_bot_table_app", default=""
)
MAX_ROWS = 200
# Each table file's module, so a bot's other code can use it without importing it again.
MODULES: dict[Path, types.ModuleType] = {}
# A bot's app label -> its name (the label is lossy: it lowercases and replaces odd characters).
BOT_NAMES: dict[str, str] = {}

# A table of a bot changed and its transaction committed. Receivers get ``bot_name`` and
# ``table`` (the model's class name); the sender is the model. Ergonaut publishes it so
# open pages re-render.
table_changed = Signal()


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

    @classmethod
    def touch(cls) -> None:
        """Announce that this table changed: call it after bulk writes (``.update()``,
        ``bulk_create``, a queryset's ``.delete()``, raw SQL), which skip the model signals,
        so pages showing the table re-render. Sent when the open transaction commits."""
        announce(cls)


def bot_name_of(model: type[models.Model]) -> str:
    label = model._meta.app_label
    return BOT_NAMES.get(label) or label.removeprefix("ergo_bot_")


class _Pending:
    """The tables changed in one transaction, announced together when it commits."""

    def __init__(self):
        self.models: dict[type[models.Model], None] = {}

    def flush(self) -> None:
        models_, self.models = list(self.models), {}
        for model in models_:
            table_changed.send(
                sender=model, bot_name=bot_name_of(model), table=model.__name__
            )


def announce(model: type[models.Model], using: str | None = None) -> None:
    """Send ``table_changed`` for ``model`` once the current transaction commits (at
    once outside one). A transaction that changes many rows announces the table once."""
    connection = transaction.get_connection(using)
    pending = getattr(connection, "_ergo_table_pending", None)
    queued = pending is not None and any(
        callback == pending.flush for _, callback, *_ in connection.run_on_commit
    )
    if queued:
        pending.models[model] = None
        return
    pending = connection._ergo_table_pending = _Pending()
    pending.models[model] = None
    transaction.on_commit(pending.flush, using=using, robust=True)


def _row_changed(sender, using=None, **_kwargs) -> None:
    if issubclass(sender, BotTable) and not sender._meta.abstract:
        announce(sender, using)


post_save.connect(_row_changed, dispatch_uid="ergo_bot_table_saved")
post_delete.connect(_row_changed, dispatch_uid="ergo_bot_table_deleted")


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
    BOT_NAMES[label] = bot_name
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
    for field in table._meta.concrete_fields:
        if field.is_relation and not safe_relation(table, field):
            continue
        kind = field.get_internal_type()
        extra = []
        if getattr(field, "null", False):
            extra.append("optional")
        if field.choices:
            extra.append("one of " + ", ".join(str(c[0]) for c in field.choices))
        name = field.attname if field.is_relation else field.name
        lines.append(f"  - {name} ({kind}{', ' + ', '.join(extra) if extra else ''})")
    return "\n".join(lines)


def _row(obj: models.Model) -> dict:
    from django.forms.models import model_to_dict

    data = model_to_dict(
        obj, fields=[field.name for field in obj._meta.concrete_fields]
    )
    for field in obj._meta.concrete_fields:
        if field.is_relation:
            if field.editable and safe_relation(type(obj), field):
                data[field.name] = getattr(obj, field.attname)
            else:
                data.pop(field.name, None)
    data["id"] = obj.pk
    for name in ("created_at", "updated_at"):
        value = getattr(obj, name, None)
        if value is not None:
            data[name] = value.isoformat(timespec="seconds")
    return {
        k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in data.items()
    }


WRITE_EXCLUDED_FIELDS = frozenset({"id", "created_at", "updated_at"})


def safe_relation(model: type[BotTable], field: models.Field) -> bool:
    """Only forward foreign keys to tables in the same bot are accessible."""
    target = field.related_model
    return bool(
        field.many_to_one
        and isinstance(target, type)
        and issubclass(target, BotTable)
        and target._meta.app_label == model._meta.app_label
    )


def table_schema(model: type[BotTable]) -> dict:
    """Expose useful model metadata without evaluating or disclosing defaults."""
    import json

    from django.core.serializers.json import DjangoJSONEncoder

    writable = set(writable_fields(model))
    fields = []
    for field in model._meta.concrete_fields:
        if field.is_relation and not safe_relation(model, field):
            continue
        info = {
            "name": field.name,
            "type": field.get_internal_type(),
            "writable": field in writable,
            "write_name": field.attname if field in writable else None,
            "required": field in writable
            and not field.blank
            and not field.null
            and not field.has_default(),
            "null": field.null,
            "has_default": field.has_default(),
            "help_text": str(field.help_text),
            "choices": list(field.flatchoices) if field.choices else [],
        }
        if field.is_relation:
            info["related_table"] = field.related_model.__name__
        fields.append(info)
    unique = [[field.attname] for field in writable_fields(model) if field.unique]
    for names in model._meta.unique_together:
        fields_for_key = [model._meta.get_field(name) for name in names]
        if all(field in writable for field in fields_for_key):
            unique.append([field.attname for field in fields_for_key])
    for constraint in model._meta.constraints:
        if (
            isinstance(constraint, models.UniqueConstraint)
            and constraint.fields
            and not constraint.condition
            and not constraint.expressions
            and not constraint.deferrable
            and not constraint.include
            and not constraint.opclasses
            and getattr(constraint, "nulls_distinct", None) is None
        ):
            fields_for_key = [model._meta.get_field(name) for name in constraint.fields]
            if all(field in writable for field in fields_for_key):
                unique.append([field.attname for field in fields_for_key])
    return json.loads(
        json.dumps(
            {"table": model.__name__, "fields": fields, "unique_keys": unique},
            cls=DjangoJSONEncoder,
        )
    )


def writable_fields(model: type[BotTable]) -> list[models.Field]:
    """Concrete fields a page or table tool may assign."""
    return [
        field
        for field in model._meta.concrete_fields
        if field.name not in WRITE_EXCLUDED_FIELDS
        and not field.primary_key
        and field.editable
        and not getattr(field, "generated", False)
        and (not field.is_relation or safe_relation(model, field))
    ]


def clean_values(model: type[BotTable], values: dict) -> dict:
    """Reject values outside the writable model fields before model validation."""
    if not isinstance(values, dict):
        msg = "values must be an object"
        raise ValueError(msg)  # noqa: TRY004 — page actions turn ValueError into a 400
    allowed = writable_fields(model)
    fields = [field.name for field in allowed]
    aliases = {field.attname: field for field in allowed}
    aliases.update({field.name: field for field in allowed})
    unknown = set(values) - aliases.keys()
    if unknown:
        msg = (
            f"{model.__name__} has no field(s) {', '.join(sorted(unknown))}. "
            f"Fields: {', '.join(fields)}"
        )
        raise ValueError(msg)
    cleaned = {}
    for name, value in values.items():
        field = aliases[name]
        if field.attname in cleaned:
            msg_0 = f"Both {field.name} and {field.attname} were supplied"
            raise ValueError(msg_0)
        cleaned[field.attname] = value
    return cleaned


def full_clean(obj: BotTable) -> None:
    """Validate ``obj`` for a write, the way a table tool or page form needs it.

    A field left at an empty default (``JSONField(default=list)``) is valid
    even without ``blank=True``: Django would call ``[]`` blank, so a row could
    never keep an empty list. A bad choice names the choices.
    """
    from django.core.exceptions import FieldDoesNotExist
    from django.core.exceptions import ValidationError

    model = type(obj)
    exclude = ["created_at", "updated_at"]
    for field in writable_fields(model):
        if field.blank or not field.has_default():
            continue
        value = getattr(obj, field.attname)
        if value in field.empty_values and value == field.get_default():
            exclude.append(field.name)
    try:
        obj.full_clean(exclude=exclude)
    except ValidationError as exc:
        if not hasattr(exc, "error_dict"):
            raise
        errors = exc.message_dict
        for name, messages in errors.items():
            try:
                field = model._meta.get_field(name)
            except FieldDoesNotExist:
                continue
            if field.choices and any(
                e.code == "invalid_choice" for e in exc.error_dict[name]
            ):
                choices = ", ".join(str(c[0]) for c in field.flatchoices)
                messages.append(f"Choices: {choices}.")
        raise ValidationError(errors) from None


def add_row(model: type[BotTable], values: dict) -> dict:
    """Create one row with the same validation used by table tools and page forms."""
    from django_ergo.bots.table_query import _safe

    obj = model(**clean_values(model, values))
    full_clean(obj)
    obj.save()
    return _safe(_row(obj))


def update_row(model: type[BotTable], id: int, values: dict) -> dict:  # noqa: A002
    """Update one row with the same validation used by table tools and page forms."""
    from django_ergo.bots.table_query import _safe

    try:
        obj = model.objects.get(pk=id)
    except model.DoesNotExist:
        msg = f"No {model.__name__} {id}"
        raise ValueError(msg) from None
    for key, value in clean_values(model, values).items():
        setattr(obj, key, value)
    full_clean(obj)
    obj.save()
    return _safe(_row(obj))


def delete_row(model: type[BotTable], id: int) -> str:  # noqa: A002
    """Delete one row, reporting a clear missing-row error to a page or tool."""
    try:
        obj = model.objects.get(pk=id)
    except model.DoesNotExist:
        msg = f"No {model.__name__} {id}"
        raise ValueError(msg) from None
    obj.delete()
    return f"Deleted {model.__name__} {id}"


def page_model(bot, table: str) -> type[BotTable]:
    """A page-writable table of ``bot``; models may opt out with ``page_writes = False``."""
    try:
        model = bot.table(table)
    except LookupError as exc:
        raise ValueError(str(exc)) from None
    if not getattr(model, "page_writes", True):
        msg = f"{model.__name__} does not allow page writes"
        raise ValueError(msg)
    return model


def _page_values(model: type[BotTable], values: dict) -> dict:
    """An empty optional FK input clears the relation."""
    if not isinstance(values, dict):
        return values
    nullable = {
        name
        for field in writable_fields(model)
        if field.is_relation and field.null and field.blank
        for name in (field.name, field.attname)
    }
    return {
        name: None if name in nullable and value == "" else value
        for name, value in values.items()
    }


def page_add(bot, table: str, values: dict) -> dict:
    model = page_model(bot, table)
    return add_row(model, _page_values(model, values))


def page_update(bot, table: str, id: int, values: dict) -> dict:  # noqa: A002
    model = page_model(bot, table)
    return update_row(model, id, _page_values(model, values))


def page_delete(bot, table: str, id: int) -> dict:  # noqa: A002
    return {"message": delete_row(page_model(bot, table), id)}


def page_delete_preview(bot, table: str, id: int) -> str:  # noqa: A002
    model = page_model(bot, table)
    try:
        row = model.objects.get(pk=id)
    except model.DoesNotExist:
        msg = f"No {model.__name__} {id}"
        raise ValueError(msg) from None
    name_field = next(
        (
            field.name
            for field in writable_fields(model)
            if field.name in ("name", "title")
        ),
        "",
    )
    label = getattr(row, name_field, "") if name_field else ""
    return (
        f"Delete {model.__name__} {label!r}"
        if label
        else f"Delete {model.__name__} {id}"
    )


def table_tools(tables: list[type[BotTable]]) -> list[BotTool]:
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
            "fields": {"type": "array", "items": {"type": "string"}},
            "offset": {"type": "integer"},
            "exclude": {"type": "object"},
            "predicate": {
                "type": "object",
                "description": 'Lookup object or {"and":[...]}, {"or":[...]}, {"not":{...}}.',
            },
        },
        required=["table"],
    )
    def query(  # noqa: PLR0913
        table: str,
        filters: dict | None = None,
        order_by: list | None = None,
        limit: int = 20,
        count_only: bool = False,
        fields: list | None = None,
        offset: int = 0,
        exclude: dict | None = None,
        predicate: dict | None = None,
    ) -> Any:
        from django_ergo.bots.table_query import query as run_query

        return run_query(
            lookup(table),
            filters,
            order_by,
            limit,
            count_only,
            fields,
            offset,
            exclude,
            predicate,
        )

    @bot_tool(
        name="ergo_table_schema",
        description="Inspect table fields, writable names, and declared unique keys.",
        parameters={"table": {"type": "string"}},
        required=["table"],
    )
    def schema(table: str) -> dict:
        return table_schema(lookup(table))

    @bot_tool(
        name="ergo_table_aggregate",
        description="Summarize rows with count, sum, avg, min, or max; optionally group by fields.",
        parameters={
            "table": {"type": "string"},
            "metrics": {
                "type": "object",
                "description": 'Alias to metric, e.g. {"n":{"op":"count"},"total":{"op":"sum","field":"price"}}.',
            },
            "group_by": {"type": "array", "items": {"type": "string"}},
            "filters": {"type": "object"},
            "exclude": {"type": "object"},
            "predicate": {
                "type": "object",
                "description": 'Lookup object or {"and":[...]}, {"or":[...]}, {"not":{...}}.',
            },
            "limit": {"type": "integer"},
        },
        required=["table", "metrics"],
    )
    def aggregate(  # noqa: PLR0913
        table: str,
        metrics: dict,
        group_by: list | None = None,
        filters: dict | None = None,
        exclude: dict | None = None,
        predicate: dict | None = None,
        limit: int = 100,
    ) -> Any:
        from django_ergo.bots.table_query import aggregate as run_aggregate

        return run_aggregate(
            lookup(table), metrics, group_by, filters, exclude, predicate, limit
        )

    @bot_tool(
        name="ergo_table_batch",
        description="Atomically add, update or upsert up to 100 rows in one table.",
        parameters={
            "table": {"type": "string"},
            "operations": {
                "type": "array",
                "items": {"type": "object"},
                "description": 'Operations: {"op":"add","values":{...}}, {"op":"update","id":1,"values":{...}}, or {"op":"upsert","match":{...},"values":{...}}. Match an entire declared unique key.',
            },
        },
        required=["table", "operations"],
    )
    def batch(table: str, operations: list) -> Any:
        from django_ergo.bots.table_batch import batch as run_batch

        return run_batch(lookup(table), operations)

    @bot_tool(
        name="ergo_table_add",
        description=f"Add a row to a table ({names}).",
        parameters={"table": {"type": "string"}, "values": {"type": "object"}},
        required=["table", "values"],
    )
    def add(table: str, values: dict) -> dict:
        return add_row(lookup(table), values)

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
        return update_row(lookup(table), id, values)

    @bot_tool(
        name="ergo_table_delete",
        description=f"Delete one row of a table ({names}) by id.",
        parameters={"table": {"type": "string"}, "id": {"type": "integer"}},
        required=["table", "id"],
        requires_approval=True,
    )
    def delete(table: str, id: int) -> str:  # noqa: A002
        return delete_row(lookup(table), id)

    return [
        fn.__bot_tool__ for fn in (query, schema, aggregate, batch, add, update, delete)
    ]
