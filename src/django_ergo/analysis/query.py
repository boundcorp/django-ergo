# ruff: noqa: C901, PLR0912, PLR2004
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
