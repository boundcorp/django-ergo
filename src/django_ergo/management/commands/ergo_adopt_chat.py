"""Promote a thread to a bot's named chat, or restore an adopted thread."""

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from django_ergo.bots import webhooks
from django_ergo.bots.adoption import adopt_chat
from django_ergo.conversation.models import ConversationSession


class Command(BaseCommand):
    help = "Adopt an existing thread as a named chat, or roll back an adoption."

    def add_arguments(self, parser):
        parser.add_argument("--bot", required=True)
        parser.add_argument("--chat", required=True)
        parser.add_argument("--session", required=True)
        parser.add_argument(
            "--user", help="Require the session owner to have this email"
        )
        parser.add_argument(
            "--bots-dir",
            default="bots",
            help="Bot folders to discover when no registry is configured",
        )
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--rollback", action="store_true")

    def handle(self, *args, **options):
        registry = webhooks.get_registry()
        if registry is None:
            from django_ergo.bots.registry import BotRegistry

            registry = BotRegistry.discover(options["bots_dir"])
        name = options["bot"]
        if registry is None or name not in registry:
            msg = f"Bot {name!r} is not loaded"
            raise CommandError(msg)
        try:
            session = ConversationSession.objects.select_related("user").get(
                pk=options["session"]
            )
        except (ConversationSession.DoesNotExist, ValidationError, ValueError) as exc:
            msg = f"No session {options['session']!r}"
            raise CommandError(msg) from exc
        if options["user"] and session.user.email != options["user"]:
            msg = "Session belongs to a different user"
            raise CommandError(msg)
        try:
            current, changed = adopt_chat(
                registry.get(name),
                options["chat"],
                session,
                dry_run=options["dry_run"],
                rollback=options["rollback"],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        action = "roll back" if options["rollback"] else "adopt"
        if not changed:
            self.stdout.write(f"Already adopted: {session.pk}")
        elif options["dry_run"]:
            meta = current.metadata or {}
            original = meta.get("adopted_from", {})
            if options["rollback"]:
                role = original["bot_role"]
                parent = original["parent_id"]
                title = original["title"]
                status = original["status"]
                prompt = "restore original prompt"
            else:
                role = "chat"
                parent = None
                title = meta.get("title")
                status = "active" if current.status == "completed" else current.status
                prompt = (
                    "preserve custom prompt"
                    if meta.get("custom_instructions")
                    else "refresh named chat prompt"
                )
            self.stdout.write(
                f"Would {action} {session.pk}: role {meta.get('bot_role')!r} -> {role!r}, "
                f"parent {str(current.parent_id) if current.parent_id else None!r} -> {parent!r}, "
                f"title {meta.get('title')!r} -> {title!r}, "
                f"status {current.status!r} -> {status!r}; {prompt}"
            )
        else:
            self.stdout.write(f"Completed {action}: {session.pk}")
