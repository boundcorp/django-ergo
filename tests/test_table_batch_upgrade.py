from __future__ import annotations

import pytest
from django.core.management import call_command
from django.db.models import QuerySet
from django.db.models.signals import post_save

from django_ergo.bots.table_batch import MAX_BATCH_OPERATIONS
from django_ergo.bots.table_batch import batch
from tests.table_upgrade_fixtures import table_upgrade_bot  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def item_model(table_upgrade_bot):  # noqa: F811
    from django_ergo.bots.runtime import Bot

    folder, engine = table_upgrade_bot
    with (folder / "tables.py").open("a") as file:
        file.write(
            "\n    sku = models.CharField(max_length=30, unique=True)\n"
            "    class Meta:\n"
            '        unique_together = [("address", "status")]\n'
            '        constraints = [models.UniqueConstraint(fields=["notes"], condition=models.Q(listed=True, notes="only"), name="batch_notes_listed_unique")]\n'
        )
    call_command("ergo_bot_makemigrations", str(folder))
    call_command("ergo_bot_migrate", str(folder))
    return Bot.load(folder, engine_factory=lambda: engine).table("House")


def test_batch_saves_each_operation_and_returns_rows(item_model):
    events = []

    def saved(sender, instance, created, **kwargs):
        events.append((instance.sku, created))

    post_save.connect(saved, sender=item_model, weak=False)
    try:
        existing = item_model.objects.create(sku="old", address="Old")
        events.clear()
        rows = batch(
            item_model,
            [
                {"op": "add", "values": {"sku": "new", "address": "New"}},
                {"op": "update", "id": existing.pk, "values": {"price": 10}},
                {"op": "upsert", "match": {"sku": "new"}, "values": {"price": 20}},
                {
                    "op": "upsert",
                    "match": {"address": "Another", "status": "new"},
                    "values": {"sku": "third"},
                },
            ],
        )
    finally:
        post_save.disconnect(saved, sender=item_model)
    assert [row["sku"] for row in rows] == ["new", "old", "new", "third"]
    assert [row["price"] for row in rows] == [None, 10, 20, None]
    assert events == [("new", True), ("old", False), ("new", False), ("third", True)]
    assert item_model.objects.get(sku="new").price == 20


def test_batch_rolls_back_every_write_and_reports_index(item_model):
    with pytest.raises(ValueError, match=r"operation 1:.*Choices"):
        batch(
            item_model,
            [
                {"op": "add", "values": {"sku": "first", "address": "First"}},
                {
                    "op": "add",
                    "values": {"sku": "bad", "address": "Bad", "status": "nope"},
                },
            ],
        )
    assert not item_model.objects.exists()


@pytest.mark.parametrize(
    "operation",
    [
        {
            "op": "upsert",
            "match": {"notes": "x"},
            "values": {"sku": "x", "address": "x"},
        },
        {"op": "upsert", "match": {"sku__exact": "x"}, "values": {}},
        {"op": "upsert", "match": {"sku": None}, "values": {}},
        {"op": "upsert", "match": {"sku": "x"}, "values": {"sku": "y"}},
        {"op": "delete", "id": 1},
        {"op": "add", "values": {}, "extra": True},
        {"op": "update", "id": 999999, "values": {}},
    ],
)
def test_invalid_operations_are_rejected(item_model, operation):
    with pytest.raises(ValueError, match="operation 0:"):
        batch(item_model, [operation])
    assert not item_model.objects.exists()


def test_batch_size_is_bounded(item_model):
    with pytest.raises(ValueError, match="operations must be a list"):
        batch(item_model, [])
    with pytest.raises(ValueError, match="operations must be a list"):
        batch(item_model, [{"op": "add", "values": {}}] * (MAX_BATCH_OPERATIONS + 1))


@pytest.mark.parametrize("race_error", ["validation", "integrity"])
def test_upsert_recovers_when_competing_insert_wins(
    item_model, monkeypatch, race_error
):
    existing = item_model.objects.create(sku="race", address="Winner")
    original_get = QuerySet.get
    missed = False

    def miss_once(queryset, *args, **kwargs):
        nonlocal missed
        if (
            queryset.model is item_model
            and queryset.query.select_for_update
            and not missed
        ):
            missed = True
            raise item_model.DoesNotExist
        return original_get(queryset, *args, **kwargs)

    if race_error == "integrity":
        from django_ergo.bots import table_batch

        original_clean = table_batch.full_clean
        first_clean = True

        def skip_first_clean(obj):
            nonlocal first_clean
            if first_clean:
                first_clean = False
            else:
                original_clean(obj)

        monkeypatch.setattr(table_batch, "full_clean", skip_first_clean)

    monkeypatch.setattr(QuerySet, "get", miss_once)
    [row] = batch(
        item_model,
        [{"op": "upsert", "match": {"sku": "race"}, "values": {"price": 12}}],
    )
    assert missed
    assert row["id"] == existing.pk
    assert item_model.objects.get(pk=existing.pk).price == 12
