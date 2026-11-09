"""Bounded, validated queries over a bot's own tables.

Predicates are lookup dictionaries or boolean trees with one of ``and``, ``or``,
``not``. Metrics map output names to ``{"op": ..., "field": ...}``; count may
omit the field to count rows. Paths may follow forward foreign keys only within
the same bot.
"""

from __future__ import annotations

import base64
import json
import re

from django.core.exceptions import FieldDoesNotExist
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Q

MAX_ROWS = 200
MAX_OFFSET = 100_000
MAX_TERMS = 100
MAX_DEPTH = 5
MAX_PATH = 5
MAX_METRICS = 20
LOOKUPS = frozenset(
    {
        "exact",
        "iexact",
        "contains",
        "icontains",
        "startswith",
        "istartswith",
        "endswith",
        "iendswith",
        "gt",
        "gte",
        "lt",
        "lte",
        "in",
        "range",
        "isnull",
    }
)
METRICS = {
    "count": models.Count,
    "sum": models.Sum,
    "avg": models.Avg,
    "min": models.Min,
    "max": models.Max,
}


def _model(model):
    from django_ergo.bots.tables import BotTable

    if (
        not isinstance(model, type)
        or not issubclass(model, BotTable)
        or model._meta.abstract  # noqa: SLF001
    ):
        msg = "Expected a concrete bot table"
        raise ValueError(msg)
    return model


def _path(model, path, *, lookup=False):  # noqa: C901, PLR0912, PLR0915
    """Resolve only concrete fields and forward, same-bot foreign keys."""
    if not isinstance(path, str) or not path or len(path) > 300:  # noqa: PLR2004
        msg = "Invalid field path"
        raise ValueError(msg)
    parts = path.split("__")
    suffix = None
    if lookup and len(parts) > 1 and parts[-1] in LOOKUPS:
        suffix = parts.pop()
    if (
        not parts
        or len(parts) > MAX_PATH
        or any(not p or p.startswith("_") for p in parts)
    ):
        msg = f"Invalid field path: {path!r}"
        raise ValueError(msg)
    current = model
    normalized = []
    for index, part in enumerate(parts):
        if isinstance(current, models.JSONField):
            # JSON keys are transforms, never model relations.
            if (
                lookup
                and suffix is None
                and index == len(parts) - 1
                and current.get_lookup(part) is not None
            ):
                msg = f"Lookup {part!r} is not allowed for {path!r}"
                raise ValueError(msg)
            normalized.append(part)
            field = current
            continue
        try:
            field = current._meta.pk if part == "pk" else current._meta.get_field(part)  # noqa: SLF001
        except FieldDoesNotExist:
            # Django's get_field accepts foreign-key attnames on supported versions;
            # explicitly resolve them where it does not.
            field = next(
                (f for f in current._meta.concrete_fields if f.attname == part),  # noqa: SLF001
                None,
            )
            if field is None:
                msg = f"Unknown field in {path!r}"
                raise ValueError(msg) from None
        if not field.concrete or field.many_to_many or field.one_to_many:
            msg = f"Only concrete forward fields are allowed: {path!r}"
            raise ValueError(msg)
        last = index == len(parts) - 1
        if field.is_relation:
            related = field.related_model
            from django_ergo.bots.tables import safe_relation

            if not safe_relation(current, field):
                msg = f"Relation must target a table of the same bot: {path!r}"
                raise ValueError(msg)
            if not last:
                if part == field.attname and field.attname != field.name:
                    msg = f"Cannot traverse an ID field: {path!r}"
                    raise ValueError(msg)
                normalized.append(field.name)
                current = related
                continue
            # Avoid Django expanding a relation's default ordering or loading objects.
            normalized.append(field.attname)
        else:
            if not last and not isinstance(field, models.JSONField):
                msg = f"Cannot traverse non-relation field: {path!r}"
                raise ValueError(msg)
            normalized.append(field.name)
            if isinstance(field, models.JSONField):
                current = field
    if suffix:
        if field.get_lookup(suffix) is None:
            msg = f"Lookup {suffix!r} is not supported for {path!r}"
            raise ValueError(msg)
        normalized.append(suffix)
    return "__".join(normalized), field, suffix


def _json_value(value, depth=0, budget=None):
    # Reject ORM expressions and cap nested data and IN lists before reaching Django.
    if depth > MAX_DEPTH:
        msg = "Filter value is too deeply nested"
        raise ValueError(msg)
    if budget is None:
        budget = [0]
    if value is None or isinstance(value, str | bool | int | float):
        return
    if isinstance(value, list | dict) and len(value) <= MAX_TERMS:
        budget[0] += len(value)
        if budget[0] > MAX_TERMS:
            msg = "Filter values must be bounded JSON values"
            raise ValueError(msg)
        for item in value.values() if isinstance(value, dict) else value:
            _json_value(item, depth + 1, budget)
        if isinstance(value, dict) and not all(isinstance(k, str) for k in value):
            msg = "Filter object keys must be strings"
            raise ValueError(msg)
        return
    msg = "Filter values must be bounded JSON values"
    raise ValueError(msg)


def _conditions(model, value, budget):
    if not isinstance(value, dict) or len(value) > MAX_TERMS:
        msg = "Filters must be a bounded object"
        raise ValueError(msg)
    result = {}
    for key, item in value.items():
        budget[0] += 1
        if budget[0] > MAX_TERMS:
            msg = "Too many predicate terms"
            raise ValueError(msg)
        path, _, suffix = _path(model, key, lookup=True)
        if path in result:
            msg = f"Duplicate filter path: {key!r}"
            raise ValueError(msg)
        _json_value(item)
        if suffix == "in" and not isinstance(item, list):
            msg = "The in lookup requires a list"
            raise ValueError(msg)
        if suffix == "range" and (not isinstance(item, list) or len(item) != 2):  # noqa: PLR2004
            msg = "The range lookup requires two values"
            raise ValueError(msg)
        if suffix == "isnull" and not isinstance(item, bool):
            msg = "The isnull lookup requires a boolean"
            raise ValueError(msg)
        result[path] = item
    return Q(**result)


def _predicate(model, value, budget, depth=0):
    if depth > MAX_DEPTH or not isinstance(value, dict) or not value:
        msg = "Invalid or too deeply nested predicate"
        raise ValueError(msg)
    budget[0] += 1
    if budget[0] > MAX_TERMS:
        msg = "Too many predicate terms"
        raise ValueError(msg)
    operators = set(value) & {"and", "or", "not"}
    if not operators:
        return _conditions(model, value, budget)
    if len(value) != 1:
        msg = "A boolean predicate must have exactly one operator"
        raise ValueError(msg)
    op = next(iter(operators))
    children = value[op]
    if op == "not":
        return ~_predicate(model, children, budget, depth + 1)
    if not isinstance(children, list) or not children or len(children) > MAX_TERMS:
        msg = f"{op} requires a nonempty bounded list"
        raise ValueError(msg)
    result = _predicate(model, children[0], budget, depth + 1)
    for child in children[1:]:
        term = _predicate(model, child, budget, depth + 1)
        result = result & term if op == "and" else result | term
    return result


def _filtered(model, filters, exclude, predicate):
    budget = [0]
    qs = model.objects.all()
    if filters is not None:
        qs = qs.filter(_conditions(model, filters, budget))
    if exclude is not None:
        qs = qs.exclude(_conditions(model, exclude, budget))
    if predicate is not None:
        qs = qs.filter(_predicate(model, predicate, budget))
    return qs


def _number(value, name, maximum, minimum=0):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        msg = f"{name} must be an integer between {minimum} and {maximum}"
        raise ValueError(msg)
    return value


def _paths(model, values, name):
    if not isinstance(values, list) or len(values) > MAX_TERMS:
        msg = f"{name} must be a bounded list"
        raise ValueError(msg)
    paths = []
    for value in values:
        path, _, _ = _path(model, value)
        if path in paths:
            msg = f"Duplicate {name} field: {value!r}"
            raise ValueError(msg)
        paths.append(path)
    return paths


class _Encoder(DjangoJSONEncoder):
    def default(self, value):
        if isinstance(value, bytes | memoryview):
            return base64.b64encode(value).decode("ascii")
        from django.db.models.fields.files import FieldFile

        if isinstance(value, FieldFile):
            return str(value)
        return super().default(value)


def _safe(value):
    return json.loads(json.dumps(value, cls=_Encoder, allow_nan=False))


def query(  # noqa: PLR0913
    model,
    filters=None,
    order_by=None,
    limit=20,
    count_only=False,
    fields=None,
    offset=0,
    exclude=None,
    predicate=None,
):
    """Return a count and a stable, bounded page of rows."""
    _model(model)
    _number(limit, "limit", MAX_ROWS, 1)
    _number(offset, "offset", MAX_OFFSET)
    qs = _filtered(model, filters, exclude, predicate)
    count = qs.count()
    if count_only:
        return {"count": count}
    ordering = []
    if order_by is not None:
        if not isinstance(order_by, list) or len(order_by) > MAX_TERMS:
            msg = "order_by must be a bounded list"
            raise ValueError(msg)
        for item in order_by:
            if not isinstance(item, str):
                msg = "Invalid ordering"
                raise ValueError(msg)  # noqa: TRY004
            descending = item.startswith("-")
            path, _, _ = _path(model, item[1:] if descending else item)
            ordering.append(("-" if descending else "") + path)
    pk = model._meta.pk.name  # noqa: SLF001
    if not any(item.lstrip("-") in {pk, model._meta.pk.attname} for item in ordering):  # noqa: SLF001
        ordering.append(pk)
    qs = qs.order_by(*ordering)
    if fields is not None:
        paths = _paths(model, fields, "fields")
        if not paths:
            msg = "fields must not be empty"
            raise ValueError(msg)
        # Preserve the caller's names, including relation names and FK attnames.
        rows = [
            dict(zip(fields, (row[path] for path in paths), strict=False))
            for row in qs.values(*paths)[offset : offset + limit]
        ]
    else:
        from django_ergo.bots.tables import _row

        rows = [_row(obj) for obj in qs[offset : offset + limit]]
    has_more = offset + len(rows) < count
    return _safe(
        {
            "count": count,
            "rows": rows,
            "has_more": has_more,
            "next_offset": offset + len(rows)
            if has_more and offset + len(rows) <= MAX_OFFSET
            else None,
        }
    )


def aggregate(  # noqa: PLR0913
    model, metrics, group_by=None, filters=None, exclude=None, predicate=None, limit=100
):
    """Aggregate matching rows, optionally returning a bounded list of groups."""
    _model(model)
    _number(limit, "limit", MAX_ROWS, 1)
    if not isinstance(metrics, dict) or not metrics or len(metrics) > MAX_METRICS:
        msg = "metrics must be a nonempty bounded object"
        raise ValueError(msg)
    groups = _paths(model, group_by, "group_by") if group_by is not None else []
    expressions = {}
    for alias, spec in metrics.items():
        if (
            not isinstance(alias, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", alias)
            or "__" in alias
            or alias in groups
            or alias in {f.name for f in model._meta.get_fields()}  # noqa: SLF001
        ):
            msg = f"Invalid metric name: {alias!r}"
            raise ValueError(msg)
        if not isinstance(spec, dict) or set(spec) - {"op", "field"}:
            msg = f"Invalid metric: {alias!r}"
            raise ValueError(msg)
        op = spec.get("op")
        if not isinstance(op, str) or op not in METRICS:
            msg = f"Unknown aggregate operation: {op!r}"
            raise ValueError(msg)
        field = spec.get("field")
        if field is None:
            if op != "count":
                msg = f"{op} requires a field"
                raise ValueError(msg)
            path = model._meta.pk.name  # noqa: SLF001
        else:
            path, resolved, _ = _path(model, field)
            if op in {"sum", "avg"} and not isinstance(
                resolved, models.IntegerField | models.FloatField | models.DecimalField
            ):
                msg = f"{op} requires a numeric field"
                raise ValueError(msg)
        expressions[alias] = METRICS[op](path)
    qs = _filtered(model, filters, exclude, predicate).order_by()
    if not groups:
        return _safe(qs.aggregate(**expressions))
    rows = qs.values(*groups).annotate(**expressions).order_by(*groups)[:limit]
    return _safe(
        {
            "groups": [
                {
                    **dict(zip(group_by, (row[p] for p in groups), strict=False)),
                    **{name: row[name] for name in expressions},
                }
                for row in rows
            ]
        }
    )
