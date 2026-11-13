from datetime import timedelta

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.utils import timezone

from django_ergo.models import LifecycleEvent

MAX_BATCH_SIZE = 10_000


class Command(BaseCommand):
    help = "Delete at most one batch of lifecycle observations older than the retention period."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=90)
        parser.add_argument("--batch-size", type=int, default=1000)

    def handle(self, *args, **options):
        days = options["days"]
        batch_size = options["batch_size"]
        if days < 1 or not 1 <= batch_size <= MAX_BATCH_SIZE:
            msg = "days must be positive and batch-size must be between 1 and 10000"
            raise CommandError(msg)
        cutoff = timezone.now() - timedelta(days=days)
        ids = list(
            LifecycleEvent.objects.filter(observed_at__lt=cutoff)
            .order_by("observed_at", "id")
            .values_list("id", flat=True)[:batch_size]
        )
        count, _ = LifecycleEvent.objects.filter(
            id__in=ids, observed_at__lt=cutoff
        ).delete()
        self.stdout.write(f"Deleted {count} lifecycle events")
