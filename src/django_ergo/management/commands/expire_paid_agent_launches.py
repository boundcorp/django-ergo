"""Local accounting recovery only; never sends inference or releases a hold."""

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from django_ergo.conversation import spend_guard
from django_ergo.conversation.models import PaidAgentLaunch


class Command(BaseCommand):
    help = "Revoke expired proxy launches and mark orphaned attempts unknown"

    def handle(self, **options):
        launches = PaidAgentLaunch.objects.filter(
            Q(expires_at__lte=timezone.now()) | Q(revoked_at__isnull=False)
        )
        count = 0
        for launch in launches:
            if launch.revoked_at is None:
                launch.revoked_at = timezone.now()
                launch.save(update_fields=["revoked_at"])
            # Revocation can happen on the last reservation while it is still
            # legitimately streaming. Only recover attempts after token expiry.
            if launch.expires_at > timezone.now():
                continue
            for row in launch.requests.filter(outcome="pending"):
                spend_guard._finish(  # noqa: SLF001 - shared durable accounting transition
                    row.pk, outcome="error", cost_status="unknown", cost_usd=None
                )
                count += 1
        self.stdout.write(f"Recovered {count} unknown holds; no reservations released")
