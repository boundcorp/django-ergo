"""Reconcile expired app receipts after controller-owned pod/PVC cleanup."""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from django_ergo.analysis.client import configured_client
from django_ergo.analysis.models import AnalysisWorkspace


class Command(BaseCommand):
    help = "Remove expired analysis receipts only after the isolated controller confirms retirement"

    def handle(self, *args, **options):
        client = configured_client()
        days = int(getattr(settings, "ERGO_ANALYSIS_RETENTION_DAYS", 30))
        if not 1 <= days <= 365:  # noqa: PLR2004
            raise ValueError("Invalid analysis retention")  # noqa: TRY003, EM101
        cutoff = timezone.now() - timedelta(days=days)
        for workspace in (
            AnalysisWorkspace.objects.select_related("session")
            .filter(touched_at__lt=cutoff)
            .iterator()
        ):
            response = client.call(
                {
                    "action": "inspect",
                    "workspace": str(workspace.id),
                    "session": str(workspace.session_id),
                    "user": str(workspace.session.user_id),
                }
            )
            if response.get("state") == "retired":
                # Exports are independent ConversationAttachments, never cascade targets.
                # Keep workspace tombstone so concurrent stale handles cannot resurrect it.
                workspace.analysisjob_set.all().delete()
                workspace.retired = True
                workspace.save(update_fields=["retired"])
