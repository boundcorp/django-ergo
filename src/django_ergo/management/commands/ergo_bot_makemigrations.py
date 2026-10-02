"""Write migrations for a bot's tables into its folder's migrations/.

python -m django ergo_bot_makemigrations path/to/bot [--name add_houses] [--check]
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError


class Command(BaseCommand):
    help = "Write migrations for a bot folder's tables (bot.yaml tables:) into its migrations/."

    def add_arguments(self, parser):
        parser.add_argument("folder", help="The bot folder (with bot.yaml)")
        parser.add_argument("--name", default="", help="Name for the new migration")
        parser.add_argument(
            "--check",
            action="store_true",
            help="Exit non-zero if a migration is missing",
        )

    def handle(self, *args, folder, name, check, **options):
        from django_ergo.bots.definition import BotDefinition
        from django_ergo.bots.tables import app_label_for
        from django_ergo.bots.tables import load_tables

        definition = BotDefinition.load(folder)
        if not definition.table_files:
            self.stdout.write(f"{definition.name} has no tables.")
            return
        load_tables(definition.name, definition.root_dir, definition.table_files)
        label = app_label_for(definition.name)
        options = {"interactive": False, "verbosity": options.get("verbosity", 1)}
        if name:
            options["name"] = name
        if check:
            options.update(check_changes=True, dry_run=True)
        try:
            call_command("makemigrations", label, **options)
        except SystemExit as exc:
            if check:
                msg = f"{definition.name}'s tables have changes without a migration"
                raise CommandError(msg) from exc
            raise
