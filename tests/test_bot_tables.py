from __future__ import annotations

import textwrap

import pytest
from django.core.management import call_command
from django.db import connection

from tests.test_bots import write_bot
from tests.test_conversation_structured import claude_engine

TABLES = textwrap.dedent(
    '''
    from django.db import models
    from django_ergo.bots import BotTable


    class House(BotTable):
        """Houses seen for the property search."""

        address = models.CharField(max_length=200)
        price = models.IntegerField(null=True, blank=True)
        status = models.CharField(max_length=20, choices=[("new", "New"), ("sold", "Sold")], default="new")
        listed = models.BooleanField(default=True)
        notes = models.TextField(blank=True)
    '''
)


@pytest.fixture
def realty(tmp_path):
    folder = write_bot(
        tmp_path,
        "name: realty\ntools: [tools/pantry.py]\ntables: [tables.py]\n",
        name="realty",
    )
    (folder / "tables.py").write_text(TABLES)
    yield folder, claude_engine()
    with connection.schema_editor() as editor:
        from django.apps import apps

        for model in list(apps.all_models.get("ergo_bot_realty", {}).values()):
            if model._meta.db_table in connection.introspection.table_names():
                editor.delete_model(model)
    from django.db.migrations.recorder import MigrationRecorder

    MigrationRecorder.Migration.objects.filter(app="ergo_bot_realty").delete()


@pytest.mark.django_db(transaction=True)
def test_tables_get_migrations_in_the_bot_folder_and_tools(realty):
    from django_ergo.bots.runtime import Bot

    folder, engine = realty
    call_command("ergo_bot_makemigrations", str(folder), "--name", "houses")
    migrations = sorted(p.name for p in (folder / "migrations").glob("0*.py"))
    assert migrations == ["0001_houses.py"]
    assert "CreateModel" in (folder / "migrations" / "0001_houses.py").read_text()

    call_command("ergo_bot_migrate", str(folder))
    assert "ergo_bot_realty_house" in connection.introspection.table_names()

    bot = Bot.load(folder, engine_factory=lambda: engine)
    [house] = bot.tables
    assert house._meta.app_label == "ergo_bot_realty"
    skill = next(d for d in bot.skill_defs if d.name == "tables")
    assert "House: Houses seen for the property search." in skill.instructions
    assert "price (IntegerField, optional)" in skill.instructions

    from django_ergo.bots.tools import FunctionToolkit

    toolkit = FunctionToolkit(skill.toolkits(None)[0].tools.values())
    added = toolkit.execute_tool(
        "ergo_table_add",
        {"table": "House", "values": {"address": "12 Oak St", "price": 450000}},
    )
    assert "12 Oak St" in str(added)
    toolkit.execute_tool(
        "ergo_table_add", {"table": "house", "values": {"address": "9 Elm Ave"}}
    )
    found = toolkit.execute_tool(
        "ergo_table_query", {"table": "House", "filters": {"price__lte": 500000}}
    )
    assert '"count": 1' in found
    assert toolkit.requires_approval("ergo_table_delete")
    with pytest.raises(ValueError, match="has no field"):
        toolkit.execute_tool(
            "ergo_table_add", {"table": "House", "values": {"color": "red"}}
        )

    # A model change gets the next migration, ready to go in the same commit.
    (folder / "tables.py").write_text(
        TABLES + "    beds = models.IntegerField(default=0)\n"
    )
    call_command("ergo_bot_makemigrations", str(folder), "--name", "beds")
    assert sorted(p.name for p in (folder / "migrations").glob("0*.py")) == [
        "0001_houses.py",
        "0002_beds.py",
    ]
    assert "AddField" in (folder / "migrations" / "0002_beds.py").read_text()
