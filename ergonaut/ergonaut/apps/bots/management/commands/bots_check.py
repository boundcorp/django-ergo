import os

from django.core.management.base import BaseCommand, CommandError

from ergonaut.apps.bots.loading import ErgonautConfigError, bot_paths, find_setup, load_registry

SECRET_KEYS = ("api_key_env", "token_env", "secret_env")


def missing_secrets(bot) -> list[str]:
    names = [bot.definition.api_key_env]
    names += [spec.config.get(key) for spec in bot.definition.plugins for key in SECRET_KEYS]
    return sorted({n for n in names if n and not os.environ.get(n)})


class Command(BaseCommand):
    help = "Load every bot, list its tools and plugins, and report missing secrets."

    def handle(self, *args, **options):
        try:
            setup = find_setup()
            registry = load_registry(setup)
        except (ErgonautConfigError, ValueError, ImportError) as e:
            raise CommandError(str(e)) from e
        if not setup.folders:
            paths = ", ".join(str(p) for p in bot_paths())
            raise CommandError(f"No bots found in {paths}. Set ERGONAUT_BOTS to a bot folder.")
        problems = 0
        for bot in registry:
            tools = sorted(t.name for m in bot.tool_modules for t in m.tools)
            plugins = [p.name for p in bot.plugins]
            self.stdout.write(f"{bot.name} ({bot.definition.root_dir})")
            self.stdout.write(f"  tools: {', '.join(tools) or 'none'}")
            self.stdout.write(f"  plugins: {', '.join(plugins) or 'none'}")
            missing = missing_secrets(bot)
            if missing:
                problems += 1
                self.stdout.write(self.style.WARNING(f"  missing secrets: {', '.join(missing)}"))
        people = ", ".join(sorted(setup.people)) or "none"
        self.stdout.write(f"people: {people}")
        if problems:
            raise CommandError("Some bots are missing secrets")
        self.stdout.write(self.style.SUCCESS("All bots loaded"))
