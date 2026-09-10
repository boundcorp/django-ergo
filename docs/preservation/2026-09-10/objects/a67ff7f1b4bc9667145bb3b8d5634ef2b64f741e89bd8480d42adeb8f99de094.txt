import json

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from django_ergo.filesystem_kb import FilesystemKBError
from django_ergo.filesystem_kb import validate_filesystem_kb


class Command(BaseCommand):
    help = "Validate a portable filesystem knowledge base."

    def add_arguments(self, parser):
        parser.add_argument("root")
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **options):
        try:
            result = validate_filesystem_kb(options["root"])
        except FilesystemKBError as exc:
            raise CommandError(str(exc)) from exc
        rendered = (
            json.dumps(result, indent=2, sort_keys=True)
            if options["json"]
            else " ".join(f"{key}={value}" for key, value in result.items())
        )
        self.stdout.write(self.style.SUCCESS(rendered))
