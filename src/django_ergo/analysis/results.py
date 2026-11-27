# ruff: noqa: TRY003, EM101, PLR2004
"""Validate untrusted sandbox data before it becomes an application receipt.

These checks establish shape and bounds, never truth or provenance of results.
Arbitrary Python has the executor's UID and can forge its output via /proc.
"""

import re

from django_ergo.analysis.protocol import ProtocolError
from django_ergo.analysis.protocol import encode


def dataset_result(value):
    version = value.get("version")
    schema = value.get("schema")
    if (
        not isinstance(version, str)
        or not re.fullmatch(r"[a-f0-9]{64}", version)
        or type(value.get("rows")) is not int
        or not 0 <= value["rows"] < 2**63
        or not isinstance(schema, list)
        or not 1 <= len(schema) <= 128
        or any(
            not isinstance(column, dict)
            or set(column) != {"name", "type"}
            or not isinstance(column["name"], str)
            or not 1 <= len(column["name"].encode()) <= 256
            or not isinstance(column["type"], str)
            or not 1 <= len(column["type"].encode()) <= 256
            for column in schema
        )
    ):
        raise ProtocolError("Malformed dataset result")
    if len({column["name"].casefold() for column in schema}) != len(schema):
        raise ProtocolError("Ambiguous dataset schema")


def select_result(value, *, columns, source_rows, offset, limit):
    rows = value.get("rows")
    total = value.get("total_rows")
    next_offset = value.get("next_offset")
    if (
        value.get("columns") != list(columns)
        or not isinstance(rows, list)
        or len(rows) > limit
        or any(
            not isinstance(row, list)
            or len(row) != len(columns)
            or any(
                cell is not None and type(cell) not in (str, int, bool) for cell in row
            )
            for row in rows
        )
        or type(total) is not int
        or not 0 <= total < 2**63
        or (rows and offset + len(rows) > total)
        or type(value.get("source_rows")) is not int
        or value["source_rows"] != source_rows
        or value.get("count_kind") != "exact"
        or type(value.get("truncated")) is not bool
        or value["truncated"] != (next_offset is not None)
        or (
            next_offset is not None
            and (
                type(next_offset) is not int
                or not rows
                or next_offset != offset + len(rows)
                or not next_offset < total
                or next_offset > 10**9
            )
        )
    ):
        raise ProtocolError("Malformed select result")
    encode(rows, limit=44 * 1024)
