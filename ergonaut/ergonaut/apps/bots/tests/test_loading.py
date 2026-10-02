import json
import textwrap

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from django_ergo.bots import webhooks
from ergonaut.apps.bots.loading import ErgonautConfigError
from ergonaut.apps.bots.loading import find_setup
from ergonaut.apps.bots.loading import load_registry
from ergonaut.apps.bots.loading import sync_people
from ergonaut.cli import main

KITCHEN = """
name: kitchen
engine: {type: claude, api_key_env: TEST_KITCHEN_KEY}
orchestration: false
tools: [tools/pantry.py]
plugins:
  - name: telegram
    token_env: TEST_KITCHEN_TELEGRAM
people:
  lee: {telegram: 111, timezone: America/Los_Angeles, name: Lee Bound, email: lee@example.com}
"""

TOOLS = """
from django_ergo.bots import bot_tool

@bot_tool
def pantry_count(item: str) -> int:
    return 4
"""


def make_bot(folder, yaml_text=KITCHEN, name="kitchen"):
    bot = folder / name
    (bot / "tools").mkdir(parents=True)
    (bot / "agents.md").write_text("You run the kitchen.")
    (bot / "tools" / "pantry.py").write_text(textwrap.dedent(TOOLS))
    (bot / "bot.yaml").write_text(textwrap.dedent(yaml_text))
    return bot


def test_setup_from_a_bot_folder_a_host_file_or_a_parent(tmp_path):
    bot = make_bot(tmp_path)
    assert find_setup([bot]).folders == [bot]
    assert find_setup([tmp_path]).folders == [bot]
    assert find_setup([tmp_path / "missing"]).folders == []

    host = tmp_path / "host"
    host.mkdir()
    (host / "ergonaut.yaml").write_text("bots: [../kitchen]\npeople:\n  aud: {telegram: 222}\n")
    setup = find_setup([host])
    assert setup.folders == [bot.resolve()]
    assert set(setup.people) == {"aud", "lee"}
    assert setup.people["lee"].timezone == "America/Los_Angeles"

    (host / "ergonaut.yaml").write_text("bots: [nope]\n")
    with pytest.raises(ErgonautConfigError, match="No bot.yaml"):
        find_setup([host])


def test_people_reach_the_telegram_plugin(tmp_path):
    setup = find_setup([make_bot(tmp_path)])
    registry = load_registry(setup)
    assert registry.get("kitchen").plugin("telegram").users == {"111": "lee"}


@pytest.mark.django_db
def test_sync_people_creates_and_updates_users(tmp_path):
    setup = find_setup([make_bot(tmp_path)])
    assert sync_people(setup) == ["lee"]
    assert sync_people(setup) == []
    lee = get_user_model().objects.get(username="lee")
    assert (lee.first_name, lee.last_name, lee.email) == ("Lee", "Bound", "lee@example.com")
    assert lee.timezone == "America/Los_Angeles"
    assert lee.telegram_id == 111
    assert not lee.has_usable_password()


def test_check_lists_bots_and_missing_secrets(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ERGONAUT_BOTS", str(make_bot(tmp_path)))
    monkeypatch.setenv("TEST_KITCHEN_KEY", "k")
    monkeypatch.delenv("TEST_KITCHEN_TELEGRAM", raising=False)
    with pytest.raises(CommandError, match="missing secrets"):
        call_command("bots_check")
    out = capsys.readouterr().out
    assert "kitchen (" in out
    assert "tools: pantry_count" in out
    assert "missing secrets: TEST_KITCHEN_TELEGRAM" in out

    monkeypatch.setenv("TEST_KITCHEN_TELEGRAM", "123:abc")
    call_command("bots_check")
    assert "All bots loaded" in capsys.readouterr().out

    monkeypatch.setenv("ERGONAUT_BOTS", str(tmp_path / "missing"))
    with pytest.raises(CommandError, match="No bots found"):
        call_command("bots_check")


@pytest.mark.django_db
def test_webhooks_load_the_bots_on_first_request(tmp_path, monkeypatch, client):
    monkeypatch.setenv("ERGONAUT_BOTS", str(make_bot(tmp_path)))
    monkeypatch.setenv("TEST_KITCHEN_TELEGRAM", "123:abc")
    webhooks.set_registry(load_registry)
    try:
        body = json.dumps({"update_id": 1})
        wrong = client.post("/hooks/kitchen/telegram/update/", body, content_type="application/json")
        assert wrong.status_code == 403  # the bot loaded; the secret header is missing
        assert client.post("/hooks/other/telegram/update/", body, content_type="application/json").status_code == 404
    finally:
        webhooks.set_registry(load_registry)


def test_cli_help_and_unknown_command(capsys):
    assert main([]) == 0
    assert "ergonaut web" in capsys.readouterr().out
    assert main(["nope"]) == 2


def test_nested_bot_folders_are_all_loaded(tmp_path):
    root = tmp_path / "config"
    (root / "kitchen" / "skills").mkdir(parents=True)
    (root / "bot.yaml").write_text("name: boundcorp\nengine: {type: claude}\n")
    (root / "agents.md").write_text("You coordinate.")
    (root / "kitchen" / "bot.yaml").write_text("name: kitchen\nengine: {type: claude}\norchestration: false\n")
    (root / "kitchen" / "agents.md").write_text("You run the kitchen.")
    setup = find_setup([root])
    assert setup.folders == [root.resolve(), (root / "kitchen").resolve()]
    registry = load_registry(setup)
    assert registry.get("kitchen").parent_name == "boundcorp"
