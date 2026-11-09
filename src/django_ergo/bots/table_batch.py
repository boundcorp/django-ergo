"""Atomic, bounded writes to a single bot table."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db import models
from django.db import router
from django.db import transaction

from django_ergo.bots.table_query import _safe
from django_ergo.bots.tables import BotTable
from django_ergo.bots.tables import _row
from django_ergo.bots.tables import clean_values
from django_ergo.bots.tables import full_clean

MAX_BATCH_OPERATIONS = 100


def _unique_keys(model: type[BotTable]) -> set[frozenset[str]]:
    """Return writable field sets backed by immediate, unconditional uniqueness."""
    fields = {field.name: field.attname for field in model._meta.concrete_fields}  # noqa: SLF001
    keys = {
        frozenset((field.attname,))
        for field in model._meta.concrete_fields  # noqa: SLF001
        if field.unique
    }
    for names in model._meta.unique_together:  # noqa: SLF001
        keys.add(frozenset(fields[name] for name in names))
    for constraint in model._meta.constraints:  # noqa: SLF001
        if (
            isinstance(constraint, models.UniqueConstraint)
            and constraint.fields
            and not constraint.expressions
            and constraint.condition is None
            and not constraint.include
            and not constraint.opclasses
            and getattr(constraint, "nulls_distinct", None) is None
            and constraint.deferrable is None
        ):
            keys.add(frozenset(fields[name] for name in constraint.fields))
    return keys


def _upsert(model, manager, using: str, operation: dict) -> dict:
    match = clean_values(model, operation.get("match"))
    values = clean_values(model, operation.get("values"))
    if not match or frozenset(match) not in _unique_keys(model):
        msg = "match must contain exactly the fields of an unconditional, immediate unique key"
        raise ValueError(msg)
    if any(value is None for value in match.values()):
        msg = "upsert match values cannot be null"
        raise ValueError(msg)
    if any(key in values and values[key] != value for key, value in match.items()):
        msg = "values cannot change an upsert match field"
        raise ValueError(msg)

    def update(obj):
        for key, value in values.items():
            setattr(obj, key, value)
        full_clean(obj)
        obj.save(using=using)
        return _row(obj)

    try:
        obj = manager.select_for_update().get(**match)
    except model.DoesNotExist:
        # The savepoint keeps the surrounding batch usable if another writer inserts
        # this key between the lookup and our insert. Validation can observe that
        # writer first, or the database can reject our insert after waiting for it.
        try:
            with transaction.atomic(using=using):
                obj = model(**(match | values))
                full_clean(obj)
                obj.save(using=using, force_insert=True)
        except (IntegrityError, ValidationError) as exc:
            try:
                obj = manager.select_for_update().get(**match)
            except model.DoesNotExist:
                raise exc from None
            return update(obj)
        return _row(obj)
    return update(obj)


def _execute(model, manager, using: str, operation: dict) -> dict:
    if not isinstance(operation, dict):
        msg = "operation must be an object"
        raise ValueError(msg)  # noqa: TRY004
    op = operation.get("op")
    allowed = {
        "add": {"op", "values"},
        "update": {"op", "id", "values"},
        "upsert": {"op", "match", "values"},
    }
    if not isinstance(op, str) or op not in allowed:
        msg = "op must be add, update, or upsert"
        raise ValueError(msg)
    if set(operation) != allowed[op]:
        msg = f"{op} requires exactly: {', '.join(sorted(allowed[op]))}"
        raise ValueError(msg)
    if op == "upsert":
        return _upsert(model, manager, using, operation)
    values = clean_values(model, operation["values"])
    if op == "add":
        obj = model(**values)
    else:
        try:
            obj = manager.select_for_update().get(pk=operation["id"])
        except model.DoesNotExist:
            msg = f"No {model.__name__} {operation['id']}"
            raise ValueError(msg) from None
        for key, value in values.items():
            setattr(obj, key, value)
    full_clean(obj)
    obj.save(using=using)
    return _row(obj)


def batch(model: type[BotTable], operations: list[dict]) -> list[dict]:
    """Apply up to 100 operations in order; any failure rolls back the whole batch.

    Each operation is an add with ``values``, update with ``id`` and ``values``,
    or upsert with a unique-key ``match`` and ``values``. Errors identify the
    zero-based operation index. Each successful write calls the model's save().
    """
    if (
        not isinstance(model, type)
        or not issubclass(model, BotTable)
        or model._meta.abstract  # noqa: SLF001
    ):
        msg = "model must be a concrete bot table"
        raise ValueError(msg)
    if (
        not isinstance(operations, list)
        or not 1 <= len(operations) <= MAX_BATCH_OPERATIONS
    ):
        msg = f"operations must be a list of 1 to {MAX_BATCH_OPERATIONS} items"
        raise ValueError(msg)
    using = router.db_for_write(model)
    manager = model._base_manager.db_manager(using)  # noqa: SLF001
    results = []
    index = 0
    try:
        with transaction.atomic(using=using):
            for index, operation in enumerate(operations):
                try:
                    results.append(_safe(_execute(model, manager, using, operation)))
                except Exception as exc:
                    msg = f"operation {index}: {exc}"
                    raise ValueError(msg) from exc
    except IntegrityError as exc:
        # An unrelated deferred database constraint can fail at commit instead of save.
        msg = f"batch commit after operation {index}: {exc}"
        raise ValueError(msg) from exc
    return results
