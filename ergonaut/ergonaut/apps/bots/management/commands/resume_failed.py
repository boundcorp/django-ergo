"""Resume every chat whose latest turn failed, e.g. after topping up API credits.

ergonaut manage resume_failed                 # failures in the last 24 hours
ergonaut manage resume_failed --hours 2 --kind credits --dry-run
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Resume chats whose latest turn failed (see the Resume button in the chat)."

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=float, default=24, help="Only failures this recent")
        parser.add_argument("--kind", default="", help="Only this kind of failure, e.g. credits")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, hours, kind, dry_run, **options):
        from django_ergo.conversation.models import ConversationSession

        from ergonaut.apps.bots.errors import describe_error
        from ergonaut.apps.bots.tasks import resume_session

        since = timezone.now() - timezone.timedelta(hours=hours)
        resumed = 0
        sessions = ConversationSession.objects.filter(
            structured_calls__status="failed", structured_calls__updated_at__gte=since
        ).distinct()
        for session in sessions:
            call = session.structured_calls.filter(kind="chat_reply").order_by("-created_at").first()
            if call is None or call.status != "failed" or (call.metadata or {}).get("resumed"):
                continue
            problem = describe_error(call.error)
            if kind and problem["kind"] != kind:
                continue
            title = (session.metadata or {}).get("title") or session.bot_name
            self.stdout.write(f"{'would resume' if dry_run else 'resuming'} {title} ({session.id}): {problem['title']}")
            if not dry_run and resume_session(session) is not None:
                resumed += 1
        if not dry_run:
            self.stdout.write(f"resumed {resumed}")
