"""Bot folders with real tables for the page tests."""

import json
import shutil
import textwrap
from pathlib import Path

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django_ergo.bots import webhooks

from ergonaut.apps.bots.loading import load_registry
from ergonaut.apps.bots.tests.fakes import fake_registry

FIXTURES = Path(__file__).parent / "fixtures"


def drop_bot_tables(label: str) -> None:
    """Remove a test bot's tables and migration records (the test database outlives the test)."""
    with connection.schema_editor() as editor:
        for model in list(apps.all_models.get(label, {}).values()):
            if model._meta.db_table in connection.introspection.table_names():
                editor.delete_model(model)
    MigrationRecorder.Migration.objects.filter(app=label).delete()


def write_bot(folder: Path, yaml: str, *, tables: str = "", tools: str = "", pages: dict[str, str] | None = None):
    """A bot folder: bot.yaml, its tables (migrated), tool file and pages/assets by relative path."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "agents.md").write_text("You run the kitchen.")
    (folder / "bot.yaml").write_text(textwrap.dedent(yaml))
    if tools:
        (folder / "tools").mkdir(exist_ok=True)
        (folder / "tools" / "pantry.py").write_text(textwrap.dedent(tools))
    if tables:
        (folder / "tables.py").write_text(textwrap.dedent(tables))
        call_command("ergo_bot_makemigrations", str(folder))
        call_command("ergo_bot_migrate", str(folder))
    for relative, text in (pages or {}).items():
        (folder / relative).parent.mkdir(parents=True, exist_ok=True)
        (folder / relative).write_text(textwrap.dedent(text))
    return folder


@pytest.fixture
def install(tmp_path):
    """``install(folder, label)`` serves the bot folder (a fake engine) and drops its tables after."""
    labels = []

    def run(folder: Path, label: str):
        labels.append(label)
        registry, client = fake_registry(folder)
        webhooks.set_registry(registry)
        return registry, client

    yield run
    webhooks.set_registry(load_registry)
    for label in labels:
        drop_bot_tables(label)


@pytest.fixture
def cook(client):
    user = get_user_model().objects.create_user("cook", "cook@example.com", "pw")
    response = client.post(
        "/api/auth/login", json.dumps({"username": "cook", "password": "pw"}), content_type="application/json"
    )
    assert response.status_code == 200
    return user


def post(client, url, data=None):
    return client.post(url, json.dumps(data or {}), content_type="application/json")


def copy_fixture(name: str, to: Path) -> Path:
    shutil.copytree(FIXTURES / name, to, dirs_exist_ok=True)
    return to
