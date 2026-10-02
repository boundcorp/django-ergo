"""The ``ergonaut`` command.

ergonaut up       # everything in one process tree; see ergonaut/up.py
ergonaut web      # migrate, then serve the web app, API, admin and webhooks
ergonaut worker   # Celery worker
ergonaut beat     # Celery beat
ergonaut bots     # every bot's long-running plugins
ergonaut check    # load the bots and report problems
ergonaut chat BOT # chat with a bot's root session in the terminal
ergonaut manage … # any manage.py command
"""

import os
import subprocess
import sys

USAGE = __doc__


def manage(*args: str) -> int:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ergonaut.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(["ergonaut", *args])
    return 0


def run(*args: str) -> int:
    return subprocess.call(list(args))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(USAGE)
        return 0
    command, rest = argv[0], argv[1:]
    if command != "up":
        from ergonaut.up import attach

        attach()  # an ``ergonaut up`` running here: use its database, broker and storage
    port = os.environ.get("PORT", "8000")
    if command == "up":
        from ergonaut.up import up

        return up(rest)
    if command == "web":
        manage("migrate", "--noinput")
        return run(
            sys.executable, "-m", "uvicorn", "ergonaut.asgi:application", "--host", "0.0.0.0", "--port", port, *rest
        )
    if command == "worker":
        return run(sys.executable, "-m", "celery", "-A", "ergonaut", "worker", "-l", "info", *rest)
    if command == "beat":
        return run(sys.executable, "-m", "celery", "-A", "ergonaut", "beat", "-l", "info", *rest)
    if command == "bots":
        return manage("bots_serve", *rest)
    if command == "check":
        return manage("bots_check", *rest)
    if command == "chat":
        return manage("bots_chat", *rest)
    if command == "manage":
        return manage(*rest)
    print(f"Unknown command {command!r}\n{USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
