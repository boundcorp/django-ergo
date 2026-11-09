from __future__ import annotations

import json

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command

from tests.test_bot_tables import realty  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)


def test_goal_progress_fk_writes_and_schema(realty):  # noqa: F811
    from django_ergo.bots.runtime import Bot
    from django_ergo.bots.table_query import query
    from django_ergo.bots.tables import add_row
    from django_ergo.bots.tables import clean_values
    from django_ergo.bots.tables import table_schema
    from django_ergo.bots.tables import update_row

    folder, engine = realty
    with (folder / "tables.py").open("a") as f:
        f.write("""\n
class GoalProgress(BotTable):
    goal = models.ForeignKey("StrategicGoal", on_delete=models.CASCADE)
    owner = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.CASCADE)
    notes = models.TextField(blank=True)
    private = models.CharField(max_length=20, default="secret", editable=False)

class StrategicGoal(BotTable):
    title = models.CharField(max_length=80, unique=True)
""")
    call_command("ergo_bot_makemigrations", str(folder))
    call_command("ergo_bot_migrate", str(folder))
    bot = Bot.load(folder, engine_factory=lambda: engine)
    goal = bot.table("StrategicGoal").objects.create(title="Ship")
    progress = bot.table("GoalProgress")
    row = add_row(progress, {"goal_id": goal.pk, "notes": "Started"})
    assert row["goal"] == goal.pk
    assert json.loads(json.dumps(row))["goal"] == goal.pk
    assert "owner" not in query(progress)["rows"][0]
    with pytest.raises(ValueError, match="same bot"):
        query(progress, filters={"owner__username": "someone"})
    assert query(progress, filters={"goal__title": "Ship"})["count"] == 1
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
