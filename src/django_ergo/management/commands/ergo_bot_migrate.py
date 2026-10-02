"""Apply the migrations in bot folders' migrations/ (only what is committed there).

python -m django ergo_bot_migrate path/to/bots [more/bots ...]
"""

from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Apply each bot's own migrations (bot folders found at or under the given paths)."

    def add_arguments(self, parser):
        parser.add_argument(
            "paths", nargs="+", help="Bot folders, or folders containing them"
        )

    def handle(self, *args, paths, **options):
        from django_ergo.bots.definition import BotDefinition
        from django_ergo.bots.registry import find_bot_folders
        from django_ergo.bots.tables import app_label_for
        from django_ergo.bots.tables import load_tables

        for path in paths:
            for folder in find_bot_folders(Path(path).expanduser()):
                definition = BotDefinition.load(folder)
                if not definition.table_files:
                    continue
                load_tables(
                    definition.name, definition.root_dir, definition.table_files
                )
                if not (definition.root_dir / "migrations").is_dir():
                    self.stdout.write(
                        f"{definition.name}: tables but no migrations yet"
                    )
                    continue
                self.stdout.write(f"Migrating {definition.name}'s tables")
                call_command(
                    "migrate",
                    app_label_for(definition.name),
                    interactive=False,
                    verbosity=options.get("verbosity", 1),
                )
