from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from django_ergo.conversation.inference_ledger import reconcile_cost_ledger
from django_ergo.conversation.models import InferenceRequest

MAX_BATCH_SIZE = 10000


class Command(BaseCommand):
    help = "Retry pending local inference cost accounting, never provider requests."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=100)
        parser.add_argument(
            "--request-id", help="Retry one specific pending request UUID"
        )

    def handle(self, *args, **options):
        size = options["batch_size"]
        if not 1 <= size <= MAX_BATCH_SIZE:
            msg = "batch-size must be between 1 and 10000"
            raise CommandError(msg)
        pending = InferenceRequest.objects.filter(cost_ledger_state="pending")
        if options["request_id"]:
            pending = pending.filter(pk=options["request_id"])
        ids = list(
            pending.order_by("created_at", "pk").values_list("pk", flat=True)[:size]
        )
        failures = sum(not reconcile_cost_ledger(pk) for pk in ids)
        self.stdout.write(
            f"Processed {len(ids)} pending inference requests; {failures} failed"
        )
        if failures:
            msg = "Accounting remains pending; inspect logs and source data"
            raise CommandError(msg)
