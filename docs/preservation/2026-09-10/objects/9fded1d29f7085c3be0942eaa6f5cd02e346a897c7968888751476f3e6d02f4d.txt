import json

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from django_ergo.filesystem_kb import FilesystemKBError
from django_ergo.filesystem_kb import build_repository_kb


class Command(BaseCommand):
    help = "Build a portable filesystem knowledge base from one Git commit."

    def add_arguments(self, parser):
        parser.add_argument("repository")
        parser.add_argument("output")
        parser.add_argument("--commit", default="HEAD")
        parser.add_argument("--project")
        parser.add_argument("--name")
        parser.add_argument("--history-limit", type=int, default=100)
        parser.add_argument(
            "--session-history",
            action="append",
            default=[],
            help="Optional file or directory of JSON, JSONL, Markdown, or text sessions.",
        )
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **options):
        try:
            result = build_repository_kb(
                options["repository"],
                options["output"],
                commit=options["commit"],
                project=options["project"],
                name=options["name"],
                history_limit=options["history_limit"],
                session_paths=options["session_history"],
            )
        except FilesystemKBError as exc:
            raise CommandError(str(exc)) from exc
        rendered = (
            json.dumps(result, indent=2, sort_keys=True)
            if options["json"]
            else " ".join(f"{key}={value}" for key, value in result.items())
        )
        self.stdout.write(self.style.SUCCESS(rendered))
