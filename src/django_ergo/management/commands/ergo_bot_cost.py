"""Print the per-turn context cost of a bot folder, changing nothing.

    python -m django ergo_bot_cost path/to/bot [--chat main] [--session UUID] [--json]

Without ``--session`` the report uses a throwaway session inside a transaction
that is rolled back. No model is called and no provider token count is requested;
see ``django_ergo.bots.cost`` for what each number means.
"""

from __future__ import annotations

import json
import uuid

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Report where a bot's turn context goes (read only; no model calls)."

    def add_arguments(self, parser):
        parser.add_argument("bot", help="path to a bot folder")
        parser.add_argument("--session", help="measure this existing session instead")
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **options):
        from django_ergo.bots.cost import render_text
        from django_ergo.bots.cost import turn_cost
        from django_ergo.bots.runtime import Bot
        from django_ergo.conversation.models import ConversationSession

        try:
            bot = Bot.load(options["bot"])
        except Exception as exc:
            msg = f"No bot at {options['bot']}: {exc}"
            raise CommandError(msg) from exc
        with transaction.atomic():
            if options["session"]:
                session = ConversationSession.objects.filter(
                    pk=options["session"]
                ).first()
                if session is None:
                    msg = f"No session {options['session']}"
                    raise CommandError(msg)
            else:
                user = get_user_model().objects.create(
                    username=f"cost-{uuid.uuid4().hex[:8]}"
                )
                session = ConversationSession.objects.create(
                    user=user, bot_name=bot.name
                )
            report = turn_cost(bot, session)
            transaction.set_rollback(True)  # read only: nothing is kept
        self.stdout.write(
            json.dumps(report, indent=2) if options["json"] else render_text(report)
        )
