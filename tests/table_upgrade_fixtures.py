import pytest
from django.apps import apps
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

from tests.test_bot_tables import TABLES
from tests.test_bots import write_bot
from tests.test_conversation_structured import claude_engine


@pytest.fixture
def table_upgrade_bot(tmp_path, request):
    # Keep these migration tests independent of other tests' dynamically registered apps.
    name = "upgrade_" + request.module.__name__.rsplit("_", 2)[-2]
    folder = write_bot(
        tmp_path,
        f"name: {name}\ntools: [tools/pantry.py]\ntables: [tables.py]\n",
        name=name,
    )
    (folder / "tables.py").write_text(TABLES)
    yield folder, claude_engine()
    label = f"ergo_bot_{name}"
    with connection.schema_editor() as editor:
        for model in reversed(list(apps.all_models.get(label, {}).values())):
            if model._meta.db_table in connection.introspection.table_names():
                editor.delete_model(model)
    MigrationRecorder.Migration.objects.filter(app=label).delete()
