import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ergonaut.settings")

application = get_asgi_application()

# Without a Celery broker, pull the bot repo in a thread instead of beat (off
# unless ERGONAUT_BOTS_PULL_SECONDS is set).
from ergonaut.apps.bots.reloading import start_pulling  # noqa: E402

start_pulling()
