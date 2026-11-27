import pytest

from django_ergo.analysis.query import InvalidQueryError
from django_ergo.analysis.query import compile_predicate
from django_ergo.analysis.runner import Limits
from django_ergo.analysis.runner import RunnerUnavailableError
from django_ergo.analysis.runner import execute


def test_runner_never_falls_back():
    with pytest.raises(RunnerUnavailableError):
        execute(session_id="session", user_id="user", code="print('hello')")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"pids": 0},
        {"timeout_seconds": 301},
        {"memory_bytes": True},
        {"output_bytes": 2**21},
    ],
)
def test_invalid_limits(kwargs):
    with pytest.raises(ValueError, match="Invalid"):
        Limits(**kwargs)


def test_predicates_quote_identifiers_and_bind_values():
    query = compile_predicate(
        {
            "op": "and",
            "args": [
                {"op": "eq", "column": 'a" OR true --', "value": "x' OR true --"},
                {"op": "in", "column": "b", "values": [1, 2]},
                {"op": "is_null", "column": "b"},
            ],
        },
        {'a" OR true --', "b"},
    )
    assert query.sql == '(("a"" OR true --" = ?) AND ("b" IN (?, ?)) AND ("b" IS NULL))'
    assert query.params == ("x' OR true --", 1, 2)


@pytest.mark.parametrize(
    "query",
    [
        {"op": "eq", "column": "missing", "value": 1},
        {"op": "eq", "column": "a", "value": None},
        {"op": "in", "column": "a", "values": list(range(101))},
        {"op": "eq", "column": "a", "value": float("nan")},
        {"op": "regex", "column": "a", "value": ".*"},
        {"op": "is_null", "column": "a", "extra": "sql"},
    ],
)
def test_bad_predicates_are_rejected(query):
    with pytest.raises(InvalidQueryError):
        compile_predicate(query, {"a"})


def test_predicate_depth_is_bounded():
    query = {"op": "eq", "column": "a", "value": 1}
    for _ in range(10):
        query = {"op": "not", "arg": query}
    with pytest.raises(InvalidQueryError):
        compile_predicate(query, {"a"})
