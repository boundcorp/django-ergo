import asyncio

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from ergonaut.apps.bots.loading import load_registry


class Command(BaseCommand):
    help = "Chat with a bot's root session in the terminal."

    def add_arguments(self, parser):
        parser.add_argument("bot")
        parser.add_argument("--user", help="Username to chat as (default: the first superuser)")

    def handle(self, *args, bot, user, **options):
        registry = load_registry()
        if bot not in registry:
            names = ", ".join(b.name for b in registry) or "none"
            raise CommandError(f"No bot {bot!r}. Bots: {names}")
        User = get_user_model()
        if user:
            person = User.objects.filter(username=user).first()
        else:
            person = User.objects.filter(is_superuser=True).order_by("date_joined").first()
        if person is None:
            raise CommandError("No such user; pass --user or create a superuser")
        asyncio.run(self.chat(registry.get(bot), person))

    async def chat(self, bot, user):
        session = await bot.root_session(user)
        self.stdout.write(f"Chatting with {bot.name} as {user.username}. Ctrl-D to quit.")
        while True:
            try:
                text = await asyncio.to_thread(input, "> ")
            except EOFError:
                return
            if not text.strip():
                continue
            result = await bot.ask(session, text)
            while result.needs_approval:
                names = ", ".join(a.tool_name for a in result.approvals)
                answer = await asyncio.to_thread(input, f"Approve {names}? [y/N] ")
                result = await bot.resume(session, answer.strip().lower() in {"y", "yes"})
            self.stdout.write(result.text or f"(error: {result.error})")
            if result.suggestions:
                self.stdout.write("  suggestions: " + " / ".join(result.suggestions))
