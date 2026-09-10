from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from django_ergo.models import KnowledgeSource
from django_ergo.repository_index import RepositoryIndexError
from django_ergo.repository_index import index_repository_commit


class Command(BaseCommand):
    help = "Index committed Python and Markdown source for one knowledge source."

    def add_arguments(self, parser):
        parser.add_argument("source_id")
        parser.add_argument("--commit")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--lexical-only", action="store_true")

    def handle(self, *args, **options):
        try:
            source = KnowledgeSource.objects.get(pk=options["source_id"])
            result = index_repository_commit(
                source,
                commit=options["commit"],
                dry_run=options["dry_run"],
                semantic=not options["lexical_only"],
            )
        except (KnowledgeSource.DoesNotExist, RepositoryIndexError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                " ".join(f"{key}={value}" for key, value in result.items())
            )
        )
