import asyncio

from django.core.management.base import BaseCommand
from django_ergo.bots import webhooks

from ergonaut import runtime_report
from ergonaut.apps.bots.loading import find_setup, load_registry, sync_people
from ergonaut.observability.exporter import start_exporter


class Command(BaseCommand):
    help = "Run every bot's long-running plugins (Telegram polling, webhook setup)."

    def handle(self, *args, **options):
        start_exporter("bots")
        # Ready once every bot has loaded (a polling plugin's serve never returns).
        runtime_report.start(["bots"], ready=False)
        setup = find_setup()
        sync_people(setup)
        registry = load_registry(setup)
        webhooks.set_registry(registry)
        names = ", ".join(bot.name for bot in registry) or "none"
        self.stdout.write(f"Serving bots: {names}")
        runtime_report.mark_ready()
        asyncio.run(self.serve(registry))

    async def serve(self, registry):
        await asyncio.gather(*(bot.serve() for bot in registry))
        # Webhook plugins return once registered; stay up so a supervisor or
        # Kubernetes doesn't restart the runner in a loop.
        self.stdout.write("Plugins are set up; waiting.")
        await asyncio.Event().wait()
