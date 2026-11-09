import json

import pytest
from django.core.management import call_command

from tests.test_bot_tables import realty  # noqa: F401


@pytest.mark.django_db(transaction=True)
def test_query_and_aggregate_are_bounded_and_safe(realty):  # noqa: F811
    from django_ergo.bots.runtime import Bot
    from django_ergo.bots.table_query import aggregate
    from django_ergo.bots.table_query import query

    folder, engine = realty
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
