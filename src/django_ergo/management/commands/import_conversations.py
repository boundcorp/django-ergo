"""Management command to import conversations from external sources."""

import asyncio
import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from django_ergo.conversation.importers import ImportService

User = get_user_model()


class Command(BaseCommand):
    help = "Import conversations from Claude CLI session files or directories"

    def add_arguments(self, parser):
        parser.add_argument("source", help="Path to a .jsonl/.json file or directory")
        parser.add_argument(
            "--user", required=True, help="Username to assign sessions to"
        )

    def handle(self, *args, **options):
        username = options["user"]
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist as err:
            msg = f"User '{username}' not found"
            raise CommandError(msg) from err
        source = Path(options["source"])
        if not source.exists():
            msg = f"Source '{source}' does not exist"
            raise CommandError(msg)
        asyncio.run(self._import(source, user))

    async def _import(self, source: Path, user):
        service = ImportService()
        count = 0
        if source.is_file():
            data = self._read_file(source)
            await service.import_auto(data, user)
            count = 1
        elif source.is_dir():
            for path in sorted(source.rglob("*.jsonl")):
                try:
                    data = self._read_file(path)
                    session = await service.import_auto(data, user)
                    session.metadata["source_file"] = str(path)
                    await session.asave()
                    count += 1
                    self.stdout.write(f"  Imported: {path.name}")
                except (ValueError, json.JSONDecodeError) as e:
                    self.stderr.write(f"  Skipped {path.name}: {e}")
            for path in sorted(source.rglob("*.json")):
                if path.name.endswith("metadata.json"):
                    continue
                try:
                    data = self._read_file(path)
                    session = await service.import_auto(data, user)
                    session.metadata["source_file"] = str(path)
                    await session.asave()
                    count += 1
                    self.stdout.write(f"  Imported: {path.name}")
                except (ValueError, json.JSONDecodeError) as e:
                    self.stderr.write(f"  Skipped {path.name}: {e}")
        self.stdout.write(self.style.SUCCESS(f"Imported {count} conversation(s)"))

    def _read_file(self, path: Path) -> list[dict]:
        text = path.read_text()
        if path.suffix == ".jsonl":
            return [
                json.loads(line) for line in text.strip().splitlines() if line.strip()
            ]
        return json.loads(text)
