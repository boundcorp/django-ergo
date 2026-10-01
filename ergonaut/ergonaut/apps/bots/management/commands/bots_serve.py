import asyncio

from django.core.management.base import BaseCommand

from django_ergo.bots import webhooks
from ergonaut.apps.bots.loading import find_setup
from ergonaut.apps.bots.loading import load_registry
from ergonaut.apps.bots.loading import sync_people


class Command(BaseCommand):
    help = "Run every bot's long-running plugins (Telegram polling, webhook setup)."

    def handle(self, *args, **options):
        setup = find_setup()
        sync_people(setup)
        registry = load_registry(setup)
        webhooks.set_registry(registry)
        names = ", ".join(bot.name for bot in registry) or "none"
        self.stdout.write(f"Serving bots: {names}")
        asyncio.run(self.serve(registry))

    async def serve(self, registry):
        await asyncio.gather(*(bot.serve() for bot in registry))
        # Webhook plugins return once registered; stay up so a supervisor or
        # Kubernetes doesn't restart the runner in a loop.
        self.stdout.write("Plugins are set up; waiting.")
        await asyncio.Event().wait()
