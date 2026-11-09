from __future__ import annotations

import json

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command

from tests.table_upgrade_fixtures import table_upgrade_bot  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)


def test_goal_progress_fk_writes_and_schema(table_upgrade_bot):  # noqa: F811, PLR0915
    from django_ergo.bots.pages import TableView
    from django_ergo.bots.runtime import Bot
    from django_ergo.bots.table_batch import batch
    from django_ergo.bots.table_query import aggregate
    from django_ergo.bots.table_query import query
    from django_ergo.bots.tables import add_row
    from django_ergo.bots.tables import clean_values
    from django_ergo.bots.tables import page_add
    from django_ergo.bots.tables import page_update
    from django_ergo.bots.tables import table_schema
    from django_ergo.bots.tables import update_row

    folder, engine = table_upgrade_bot
    with (folder / "tables.py").open("a") as f:
        f.write("""\n
class GoalProgress(BotTable):
    goal = models.ForeignKey("StrategicGoal", on_delete=models.CASCADE)
    goal_title = models.ForeignKey("StrategicGoal", to_field="title", null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    hidden_goal = models.ForeignKey("StrategicGoal", null=True, blank=True, editable=False, on_delete=models.CASCADE, related_name="+")
    category = models.ForeignKey("Category", null=True, blank=True, on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    payload = models.BinaryField(null=True, blank=True, editable=True)
    owner = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.CASCADE)
    notes = models.TextField(blank=True)
    private = models.CharField(max_length=20, default="secret", editable=False)

class StrategicGoal(BotTable):
    title = models.CharField(max_length=80, unique=True)
    owner = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    metadata = models.JSONField(default=dict)

class Category(BotTable):
    code = models.CharField(max_length=30, primary_key=True)
""")
    call_command("ergo_bot_makemigrations", str(folder))
    call_command("ergo_bot_migrate", str(folder))
    bot = Bot.load(folder, engine_factory=lambda: engine)
    goal = bot.table("StrategicGoal").objects.create(
        title="Ship", metadata={"owner": {"username": "visible JSON"}}
    )
    progress = bot.table("GoalProgress")
    row = add_row(progress, {"goal_id": goal.pk, "notes": "Started"})
    assert row["goal"] == goal.pk
    assert json.loads(json.dumps(row))["goal"] == goal.pk
    assert "owner" not in query(progress)["rows"][0]
    with pytest.raises(ValueError, match="same bot"):
        query(progress, fields=["owner__username"])
    with pytest.raises(ValueError, match="same bot"):
        aggregate(progress, {"n": {"op": "count", "field": "owner__username"}})
    with pytest.raises(ValueError, match="same bot"):
        aggregate(progress, {"n": {"op": "count"}}, group_by=["owner__username"])
    with pytest.raises(ValueError, match="same bot"):
        query(progress, filters={"owner__username": "someone"})
    with pytest.raises(ValueError, match="same bot"):
        query(progress, filters={"goal__owner__username": "someone"})
    assert (
        query(progress, filters={"goal__metadata__owner__username": "visible JSON"})[
            "count"
        ]
        == 1
    )
    assert query(progress, filters={"goal__title": "Ship"})["count"] == 1
    for keyword in ("filters", "exclude", "predicate"):
        with pytest.raises(ValueError, match="Duplicate filter path"):
            query(progress, **{keyword: {"goal": goal.pk, "goal_id": goal.pk + 1}})
    category = bot.table("Category").objects.create(code="cat-1")
    updated = update_row(
        progress, row["id"], {"goal_title_id": "Ship", "category_id": category.pk}
    )
    assert updated["goal_title"] == "Ship" and updated["category"] == "cat-1"
    assert query(
        progress,
        filters={"goal_title__title": "Ship"},
        fields=["goal_title", "category"],
    )["rows"] == [{"goal_title": "Ship", "category": "cat-1"}]
    assert (
        batch(
            progress,
            [{"op": "update", "id": row["id"], "values": {"goal_title_id": "Ship"}}],
        )[0]["goal_title"]
        == "Ship"
    )
    added = page_add(bot, "GoalProgress", {"goal": goal.pk, "goal_title": ""})
    assert added["goal_title"] is None
    assert (
        page_update(bot, "GoalProgress", row["id"], {"goal_title": ""})["goal_title"]
        is None
    )
    instance = progress.objects.get(pk=row["id"])
    instance.hidden_goal = goal
    instance.amount = "12.50"
    instance.payload = b"abc"
    instance.save()
    page_row = TableView(progress).filter(pk=instance.pk).first()
    assert page_row["amount"] * 2 == 25
    assert "hidden_goal" not in page_row
    assert query(progress, filters={"pk": instance.pk})["rows"][0]["payload"] == "YWJj"
    assert (
        query(progress, filters={"pk": instance.pk}, fields=["payload"])["rows"][0][
            "payload"
        ]
        == "YWJj"
    )
    assert (
        update_row(progress, row["id"], {"goal_id": goal.pk, "notes": "Done"})["notes"]
        == "Done"
    )
    with pytest.raises(ValidationError):
        add_row(progress, {"goal_id": 99999999})
    with pytest.raises(ValueError, match="Both"):
        clean_values(progress, {"goal": goal.pk, "goal_id": goal.pk})
    with pytest.raises(ValueError):  # noqa: PT011
        clean_values(progress, {"private": "x"})
    schema = table_schema(progress)
    field = next(f for f in schema["fields"] if f["name"] == "goal")
    assert (
        field["write_name"] == "goal_id" and field["related_table"] == "StrategicGoal"
    )
    assert "secret" not in json.dumps(schema)
