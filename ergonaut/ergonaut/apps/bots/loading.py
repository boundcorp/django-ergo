"""Find the bots this Ergonaut serves and the people who use them.

``ERGONAUT_BOTS`` (default ``/bot``) is a path, or several separated by
``:``. Each path is one of:

- a bot folder (has ``bot.yaml``),
- a folder with an ``ergonaut.yaml`` listing bot folders::

      bots: [kitchen, ../sysadmin]
      people:
        lee: {telegram: 123456789, timezone: America/Los_Angeles}

- a folder of bot folders.

``people:`` can also sit in a bot's own ``bot.yaml``. Each person becomes a
Django user (created if missing, updated otherwise), and their Telegram id
is added to every Telegram plugin, so the plugins and the web app agree on
who is who.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

import yaml
from django.contrib.auth import get_user_model

from django_ergo.bots.definition import CONFIG_FILE
from django_ergo.bots.registry import BotRegistry

logger = logging.getLogger(__name__)

HOST_CONFIG = "ergonaut.yaml"
DEFAULT_BOTS = "/bot"


class ErgonautConfigError(ValueError):
    pass


@dataclass
class Person:
    username: str
    telegram: int | None = None
    timezone: str = ""
    email: str = ""
    name: str = ""


@dataclass
class Setup:
    folders: list[Path] = field(default_factory=list)
    people: dict[str, Person] = field(default_factory=dict)


def bot_paths(value: str | None = None) -> list[Path]:
    value = value if value is not None else os.environ.get("ERGONAUT_BOTS", DEFAULT_BOTS)
    return [Path(p).expanduser() for p in value.split(":") if p.strip()]


def _read_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        msg = f"{path} must be a mapping"
        raise ErgonautConfigError(msg)
    return data


def _add_people(setup: Setup, data: dict, source: Path) -> None:
    people = data.get("people") or {}
    if not isinstance(people, dict):
        msg = f"people in {source} must map usernames to details"
        raise ErgonautConfigError(msg)
    for username, details in people.items():
        details = details or {}
        telegram = details.get("telegram")
        setup.people[str(username)] = Person(
            username=str(username),
            telegram=int(telegram) if telegram is not None else None,
            timezone=str(details.get("timezone") or ""),
            email=str(details.get("email") or ""),
            name=str(details.get("name") or ""),
        )


def find_setup(paths: list[Path] | None = None) -> Setup:
    setup = Setup()
    for path in paths if paths is not None else bot_paths():
        if (path / CONFIG_FILE).is_file():
            folders = [path]
        elif (path / HOST_CONFIG).is_file():
            data = _read_yaml(path / HOST_CONFIG)
            _add_people(setup, data, path / HOST_CONFIG)
            folders = [(path / p).resolve() for p in data.get("bots") or []]
        elif path.is_dir():
            folders = sorted(p for p in path.iterdir() if (p / CONFIG_FILE).is_file())
        else:
            logger.info("No bots at %s", path)
            continue
        for folder in folders:
            if not (folder / CONFIG_FILE).is_file():
                msg = f"No {CONFIG_FILE} in {folder}"
                raise ErgonautConfigError(msg)
            _add_people(setup, _read_yaml(folder / CONFIG_FILE), folder / CONFIG_FILE)
            setup.folders.append(folder)
    return setup


def load_registry(setup: Setup | None = None) -> BotRegistry:
    setup = setup or find_setup()
    registry = BotRegistry.from_paths(setup.folders)
    telegram_ids = {str(p.telegram): p.username for p in setup.people.values() if p.telegram}
    for bot in registry:
        plugin = bot.plugin("telegram")
        if plugin is not None:
            for telegram_id, username in telegram_ids.items():
                plugin.users.setdefault(telegram_id, username)
    return registry


def sync_people(setup: Setup | None = None) -> list[str]:
    """Create or update a user per person. Returns the usernames created."""
    setup = setup or find_setup()
    User = get_user_model()
    created_names = []
    for person in setup.people.values():
        user, created = User.objects.get_or_create(username=person.username)
        if created:
            user.set_unusable_password()
            created_names.append(person.username)
        if person.email:
            user.email = person.email
        if person.name:
            first, _, last = person.name.partition(" ")
            user.first_name, user.last_name = first, last
        if person.timezone:
            user.timezone = person.timezone
        if person.telegram is not None:
            user.telegram_id = person.telegram
        user.save()
    return created_names
