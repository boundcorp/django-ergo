import json

import pytest
from django.core.management import call_command

from tests.table_upgrade_fixtures import table_upgrade_bot  # noqa: F401


@pytest.mark.django_db(transaction=True)
def test_query_and_aggregate_are_bounded_and_safe(table_upgrade_bot):  # noqa: F811
    from django_ergo.bots.runtime import Bot
    from django_ergo.bots.table_query import aggregate
    from django_ergo.bots.table_query import query

    folder, engine = table_upgrade_bot
    call_command("ergo_bot_makemigrations", str(folder), "--name", "houses")
    call_command("ergo_bot_migrate", str(folder))
    [house] = Bot.load(folder, engine_factory=lambda: engine).tables
    house.objects.create(address="Oak", price=10, status="new")
    house.objects.create(address="Elm", price=20, status="sold")
    house.objects.create(address="Ash", price=20, status="new")

    page = query(
        house, order_by=["price"], fields=["address", "price"], limit=1, offset=1
    )
    assert page == {
        "count": 3,
        "rows": [{"address": "Elm", "price": 20}],
        "has_more": True,
        "next_offset": 2,
    }
    assert query(
        house, filters={"price__gte": 20}, exclude={"status": "sold"}, count_only=True
    ) == {"count": 1}
    assert query(
        house,
        predicate={"or": [{"address": "Oak"}, {"not": {"status": "new"}}]},
        count_only=True,
    ) == {"count": 2}
    result = aggregate(
        house,
        {"total": {"op": "sum", "field": "price"}, "n": {"op": "count"}},
        group_by=["status"],
    )
    assert result == {
        "groups": [
            {"status": "new", "total": 30, "n": 2},
            {"status": "sold", "total": 20, "n": 1},
        ]
    }
    assert aggregate(house, {"average": {"op": "avg", "field": "price"}}) == {
        "average": 50 / 3
    }
    json.dumps(query(house))
    for kwargs in (
        {"filters": {"address__regex": ".*"}},
        {"filters": {"price__in": list(range(101))}},
        {"order_by": ["?"]},
        {"fields": ["address__lower"]},
        {"limit": 201},
        {"offset": -1},
        {"predicate": {"or": []}},
    ):
        with pytest.raises(ValueError):  # noqa: PT011
            query(house, **kwargs)
    with pytest.raises(ValueError):  # noqa: PT011
        aggregate(house, {"total": {"op": "sum", "field": "address"}})
    with pytest.raises(ValueError, match="metrics"):
        aggregate(house, {f"metric{i}": {"op": "count"} for i in range(21)})
    with pytest.raises(ValueError, match="limit"):
        aggregate(house, {"n": {"op": "count"}}, group_by=["status"], limit=201)


@pytest.mark.django_db(transaction=True)
def test_query_json_paths_and_predicate_bounds(table_upgrade_bot):  # noqa: F811
    from django_ergo.bots.runtime import Bot
    from django_ergo.bots.table_query import MAX_DEPTH
    from django_ergo.bots.table_query import MAX_TERMS
    from django_ergo.bots.table_query import query

    folder, engine = table_upgrade_bot
    call_command("ergo_bot_makemigrations", str(folder))
    call_command("ergo_bot_migrate", str(folder))
    [house] = Bot.load(folder, engine_factory=lambda: engine).tables
    house.objects.create(address="Oak", tags={"place": {"city": "York"}, "list": ["a"]})
    assert query(house, filters={"tags__place__city__icontains": "yor"})["count"] == 1
    assert query(house, filters={"tags__list__0": "a"})["count"] == 1
    with pytest.raises(ValueError, match="not allowed"):
        query(house, filters={"tags__place__regex": ".*"})
    predicate = {"address": "Oak"}
    for _ in range(MAX_DEPTH):
        predicate = {"not": predicate}
    assert query(house, predicate=predicate)["count"] == (0 if MAX_DEPTH % 2 else 1)
    with pytest.raises(ValueError, match="deeply nested"):
        query(house, predicate={"not": predicate})
    with pytest.raises(ValueError, match="Too many"):
        query(house, predicate={"or": [{"address": "Oak"}] * MAX_TERMS})
    with pytest.raises(ValueError, match="bounded JSON"):
        query(house, filters={"tags": {"a": list(range(MAX_TERMS))}})
