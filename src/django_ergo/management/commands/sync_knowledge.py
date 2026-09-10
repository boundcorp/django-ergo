from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from django_ergo.models import KnowledgeSource
from django_ergo.sync import KnowledgeSyncError
from django_ergo.sync import project_commit


class Command(BaseCommand):
    help = "Project committed Git Markdown into Django Ergo Articles."

    def add_arguments(self, parser):
        parser.add_argument("source_id", type=str)
        parser.add_argument("--commit", default=None)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        try:
            source = KnowledgeSource.objects.get(pk=options["source_id"])
        except KnowledgeSource.DoesNotExist as exc:
            raise CommandError(
                f"Unknown knowledge source: {options['source_id']}"
            ) from exc
        try:
            result = project_commit(
                source,
                commit=options["commit"],
                dry_run=options["dry_run"],
            )
        except KnowledgeSyncError as exc:
            raise CommandError(str(exc)) from exc
        mode = "dry-run" if options["dry_run"] else "sync"
        self.stdout.write(
            self.style.SUCCESS(
                f"{mode}: commit={result['commit']} documents={result['documents']} "
                f"writes={result['writes']}"
            )
        )
