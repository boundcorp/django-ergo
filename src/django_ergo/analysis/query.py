# ruff: noqa: C901, PLR0912, PLR2004, PLR0915, TRY003, EM101
"""Compile structured predicates for a trusted isolated dataset executor.

Identifiers come exclusively from a validated schema. Values are parameters.
This module never opens datasets or runs SQL in the application process.
"""

from dataclasses import dataclass


class InvalidQueryError(ValueError):
    """A structured query exceeded a limit or is malformed."""


@dataclass(frozen=True)
class Predicate:
    sql: str
    params: tuple


def compile_predicate(node: dict, columns: set[str]) -> Predicate:
    """Compile a small, bounded predicate tree; callers must bind params in DuckDB."""
    budget = [0]

    def compile_node(item, depth):
        budget[0] += 1
        if depth > 8 or budget[0] > 64 or not isinstance(item, dict):
            msg = "Predicate is too complex or malformed"
            raise InvalidQueryError(msg)
        op = item.get("op")
        if not isinstance(op, str):
            raise InvalidQueryError("Invalid predicate operation")
        if op in ("and", "or"):
            if set(item) != {"op", "args"} or not isinstance(item["args"], list):
                msg = "Invalid boolean predicate"
                raise InvalidQueryError(msg)
            children = item["args"]
            if not 1 <= len(children) <= 32:
                msg = "Invalid boolean predicate size"
                raise InvalidQueryError(msg)
            parts = [compile_node(child, depth + 1) for child in children]
            return Predicate(
                "(" + (f" {op.upper()} ").join(p.sql for p in parts) + ")",
                tuple(value for part in parts for value in part.params),
            )
        if op == "not":
            if set(item) != {"op", "arg"}:
                msg = "Invalid not predicate"
                raise InvalidQueryError(msg)
            part = compile_node(item["arg"], depth + 1)
            return Predicate(f"(NOT {part.sql})", part.params)
        column = item.get("column")
        if not isinstance(column, str) or column not in columns:
            msg = "Unknown column"
            raise InvalidQueryError(msg)
        name = '"' + column.replace('"', '""') + '"'
        if op in ("is_null", "not_null"):
            if set(item) != {"op", "column"}:
                msg = "Invalid null predicate"
                raise InvalidQueryError(msg)
            return Predicate(
                f"({name} IS {'NOT ' if op == 'not_null' else ''}NULL)", ()
            )
        operators = {
            "eq": "=",
            "ne": "<>",
            "lt": "<",
            "le": "<=",
            "gt": ">",
            "ge": ">=",
        }
        if op in operators:
            if set(item) != {"op", "column", "value"} or not scalar(item["value"]):
                msg = "Invalid comparison value"
                raise InvalidQueryError(msg)
            return Predicate(f"({name} {operators[op]} ?)", (item["value"],))
        if op == "in":
            if set(item) != {"op", "column", "values"}:
                msg = "Invalid membership predicate"
                raise InvalidQueryError(msg)
            values = item["values"]
            if (
                not isinstance(values, list)
                or not 1 <= len(values) <= 100
                or not all(map(scalar, values))
            ):
                msg = "Invalid membership values"
                raise InvalidQueryError(msg)
            return Predicate(
                f"({name} IN ({', '.join('?' for _ in values)}))", tuple(values)
            )
        msg = "Unsupported predicate"
        raise InvalidQueryError(msg)

    return compile_node(node, 0)


def scalar(value):
    """Allow bounded JSON scalars; NULL uses the explicit null operators."""
    import math

    if isinstance(value, str):
        return len(value.encode("utf-8")) <= 4096
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return -(2**63) <= value < 2**63
    return isinstance(value, float) and math.isfinite(value)


def quote(name, columns):
    if not isinstance(name, str) or name not in columns:
        raise InvalidQueryError("Unknown column")
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True)
class Select:
    sql: str
    params: tuple
    columns: tuple


def compile_select(spec, columns):
    """Query only registered in-memory data. Stable order includes every output column."""
    if not isinstance(spec, dict) or set(spec) - {
        "columns",
        "where",
        "group_by",
        "aggregates",
        "sort",
        "limit",
    }:
        raise InvalidQueryError("Invalid select")
    limit = spec.get("limit", 100)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise InvalidQueryError("Invalid row limit")
    group = spec.get("group_by", [])
    aggregates = spec.get("aggregates", [])
    projection = spec.get("columns", list(columns))
    if (
        not isinstance(group, list)
        or not isinstance(aggregates, list)
        or not isinstance(projection, list)
    ):
        raise InvalidQueryError("Invalid projection")
    if not all(isinstance(c, str) for c in group + projection):
        raise InvalidQueryError("Invalid column name")
    if len(group) > 32 or len(aggregates) > 32 or not 1 <= len(projection) <= 128:
        raise InvalidQueryError("Projection too wide")
    params = ()
    where = ""
    if "where" in spec:
        pred = compile_predicate(spec["where"], set(columns))
        where, params = " WHERE " + pred.sql, pred.params
    if aggregates or group:
        names = list(group)
        expressions = [quote(c, columns) for c in group]
        for agg in aggregates:
            if not isinstance(agg, dict) or set(agg) != {"op", "column", "as"}:
                raise InvalidQueryError("Invalid aggregate")
            op = agg["op"]
            if not isinstance(op, str) or op not in {
                "count",
                "sum",
                "avg",
                "min",
                "max",
                "count_distinct",
            }:
                raise InvalidQueryError("Invalid aggregate operation")
            alias = agg["as"]
            if not isinstance(alias, str) or not 1 <= len(alias) <= 100:
                raise InvalidQueryError("Invalid aggregate alias")
            col = (
                "*"
                if op == "count" and agg["column"] == "*"
                else quote(agg["column"], columns)
            )
            expression = (
                f"COUNT(DISTINCT {col})"
                if op == "count_distinct"
                else f"{op.upper()}({col})"
            )
            expressions.append(expression + " AS " + quote(alias, {alias}))
            names.append(alias)
        select = ", ".join(expressions)
        grouping = (
            " GROUP BY " + ", ".join(quote(c, columns) for c in group) if group else ""
        )
    else:
        names = projection
        select = ", ".join(quote(c, columns) for c in names)
        grouping = ""
    if not names or len({name.casefold() for name in names}) != len(names):
        raise InvalidQueryError("Duplicate or empty output columns")
    sorting = spec.get("sort", [])
    if not isinstance(sorting, list) or len(sorting) > 32:
        raise InvalidQueryError("Invalid sort")
    ordered = []
    for item in sorting:
        if (
            not isinstance(item, dict)
            or set(item) != {"column", "direction"}
            or item["direction"] not in {"asc", "desc"}
        ):
            raise InvalidQueryError("Invalid sort")
        ordered.append(
            quote(item["column"], names)
            + " "
            + item["direction"].upper()
            + " NULLS LAST"
        )
    # Identical projected rows are indistinguishable; ordering all outputs gives stable pagination.
    ordered.extend(quote(c, names) + " ASC NULLS LAST" for c in names)
    sql = f"SELECT {select} FROM data{where}{grouping}"  # noqa: S608 — quoted schema identifiers, bound values
    return Select(sql + " ORDER BY " + ", ".join(ordered), params, tuple(names))
