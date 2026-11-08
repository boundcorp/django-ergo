"""Bounded JSON values and deliberately small, non-executable workflow conditions."""

from __future__ import annotations

import ast
import json
import os
import re

from django_ergo.bots.introspection import SECRET_WORDS
from django_ergo.bots.introspection import redact

MAX_JSON_BYTES = 1_000_000
MAX_JSON_DEPTH = 32
MAX_JSON_ITEMS = 10_000
MIN_SECRET_LENGTH = 6
MAX_EXPRESSION_LENGTH = 1000
MAX_EXPRESSION_NODES = 128
TOKEN_COUNTS = frozenset(
    {
        "maxTokens",
        "max_tokens",
        "max_input_tokens",
        "inputTokenLimit",
        "outputTokenLimit",
    }
)


class WorkflowError(ValueError):
    pass


def condition_tree(expression: str) -> ast.Expression:
    if not isinstance(expression, str) or len(expression) > MAX_EXPRESSION_LENGTH:
        msg = "if needs an expression of at most 1000 characters"
        raise WorkflowError(msg)
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, RecursionError) as exc:
        msg = "invalid workflow condition"
        raise WorkflowError(msg) from exc
    allowed = (
        ast.Expression,
        ast.Name,
        ast.Load,
        ast.Attribute,
        ast.Subscript,
        ast.Constant,
        ast.List,
        ast.Tuple,
        ast.BoolOp,
        ast.And,
        ast.Or,
        ast.UnaryOp,
        ast.Not,
        ast.Compare,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
        ast.In,
        ast.NotIn,
    )
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_EXPRESSION_NODES or any(
        not isinstance(node, allowed)
        or (isinstance(node, ast.Name) and node.id.startswith("_"))
        or (isinstance(node, ast.Attribute) and node.attr.startswith("_"))
        or (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
            and node.slice.value.startswith("__")
        )
        or (
            isinstance(node, ast.Constant)
            and not isinstance(node.value, str | int | float | bool | type(None))
        )
        for node in nodes
    ):
        msg = "unsupported workflow condition; only JSON lookups and comparisons are allowed"
        raise WorkflowError(msg)
    return tree


# Keep the bounded AST interpreter together so its accepted operations are auditable.
def condition(expression: str, values: dict) -> bool:  # noqa: C901
    """Interpret JSON lookups; attributes are dictionary keys, never getattr."""

    def visit(node):  # noqa: C901, PLR0911, PLR0912 — explicit dispatch for each allowed AST operation
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.List | ast.Tuple):
            return [visit(item) for item in node.elts]
        if isinstance(node, ast.Attribute):
            value = visit(node.value)
            if type(value) is not dict:
                msg = "condition attribute lookup requires a JSON object"
                raise WorkflowError(msg)
            return value[node.attr]
        if isinstance(node, ast.Subscript):
            value, key = visit(node.value), visit(node.slice)
            if type(value) is dict and type(key) is str:
                return value[key]
            if type(value) is list and type(key) is int:
                return value[key]
            msg = "condition index requires a JSON object key or list index"
            raise WorkflowError(msg)
        if isinstance(node, ast.UnaryOp):
            return not visit(node.operand)
        if isinstance(node, ast.BoolOp):
            for item in node.values:
                result = bool(visit(item))
                if isinstance(node.op, ast.And) and not result:
                    return False
                if isinstance(node.op, ast.Or) and result:
                    return True
            return result
        if isinstance(node, ast.Compare):
            left = visit(node.left)
            for op, other in zip(node.ops, node.comparators, strict=True):
                right = visit(other)
                if isinstance(op, ast.Eq):
                    matched = left == right
                elif isinstance(op, ast.NotEq):
                    matched = left != right
                elif isinstance(op, ast.Lt):
                    matched = left < right
                elif isinstance(op, ast.LtE):
                    matched = left <= right
                elif isinstance(op, ast.Gt):
                    matched = left > right
                elif isinstance(op, ast.GtE):
                    matched = left >= right
                elif isinstance(op, ast.In):
                    matched = left in right
                else:  # NotIn: all other operators are refused before execution.
                    matched = left not in right
                if not matched:
                    return False
                left = right
            return True
        msg = "unsupported workflow condition"
        raise WorkflowError(msg)

    try:
        return bool(visit(condition_tree(expression).body))
    except (KeyError, IndexError, TypeError) as exc:
        msg = "condition refers to a missing or incompatible JSON value"
        raise WorkflowError(msg) from exc


# Validation and redaction share one bounded JSON boundary.
def safe_json(value):  # noqa: C901
    """Accept only bounded JSON, redacting credential keys and configured secrets.

    Trusted functions must return public data; this is defense in depth, not a
    promise that arbitrary unlabelled secrets can be identified.
    """
    count = 0

    def validate(item, depth=0):
        nonlocal count
        count += 1
        if depth > MAX_JSON_DEPTH or count > MAX_JSON_ITEMS:
            msg = "step result exceeds JSON depth or item limit"
            raise WorkflowError(msg)
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    msg = "step result needs string JSON keys"
                    raise WorkflowError(msg)
                validate(child, depth + 1)
        elif type(item) is list:
            for child in item:
                validate(child, depth + 1)
        elif type(item) not in (str, int, float, bool, type(None)):
            msg = "step result must be JSON"
            raise WorkflowError(msg)

    validate(value)

    def scrub(item):
        if type(item) is dict:
            return {
                key: child
                if key in TOKEN_COUNTS and type(child) is int
                else "[hidden]"
                if redact({key: None})[key] == "[hidden]"
                else scrub(child)
                for key, child in item.items()
            }
        if type(item) is list:
            return [scrub(child) for child in item]
        return item

    try:
        encoded = json.dumps(scrub(value), allow_nan=False)
    except (ValueError, OverflowError) as exc:
        msg = "step result must be finite JSON"
        raise WorkflowError(msg) from exc
    if len(encoded.encode()) > MAX_JSON_BYTES:
        msg = "step result exceeds 1000000 JSON bytes"
        raise WorkflowError(msg)
    for name, secret in os.environ.items():
        if len(secret) >= MIN_SECRET_LENGTH and any(
            word in name.lower() for word in SECRET_WORDS
        ):
            encoded = encoded.replace(json.dumps(secret)[1:-1], "[hidden]")
    encoded = re.sub(
        r"(?:sk-(?:ant-)?|gh[pousr]_)[A-Za-z0-9_-]{12,}", "[hidden]", encoded
    )
    return json.loads(encoded)


def safe_error(exc: Exception) -> str:
    """Retain only typed public catalog errors, otherwise withhold exception values."""
    from django_ergo.bots.model_catalog import ModelCatalogError

    if isinstance(exc, ModelCatalogError):
        return str(exc)
    name = type(exc).__name__
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", name):
        name = "Exception"
    return f"Step failed ({name}); exception details withheld for credential safety."
