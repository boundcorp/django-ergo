import asyncio
from pathlib import Path

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError


class Command(BaseCommand):
    help = (
        "Load Ergo bots and run their plugins' long-running work (for example "
        "Telegram polling). Each path is a bot folder or a folder of bots."
    )

    def add_arguments(self, parser):
        parser.add_argument("paths", nargs="+")
        parser.add_argument(
            "--check", action="store_true", help="Load the bots and exit."
        )

    def handle(self, *args, **options):
        from django_ergo.bots.definition import CONFIG_FILE
        from django_ergo.bots.definition import BotDefinitionError
        from django_ergo.bots.registry import BotRegistry

        registry = BotRegistry()
        try:
            for raw in options["paths"]:
                path = Path(raw)
                if (path / CONFIG_FILE).is_file() or path.is_file():
                    registry.load(path)
                elif path.is_dir():
                    for bot in BotRegistry.discover(path):
                        registry.add(bot)
                else:
                    msg = f"No bot at {path}"
                    raise CommandError(msg)
        except (BotDefinitionError, ValueError, ImportError) as exc:
            raise CommandError(str(exc)) from exc

        for bot in registry:
            plugins = ", ".join(p.name or type(p).__name__ for p in bot.plugins)
            self.stdout.write(f"{bot.name}: plugins [{plugins}]")
        if options["check"]:
            return

        async def serve():
            await asyncio.gather(*(bot.serve() for bot in registry))

        asyncio.run(serve())
