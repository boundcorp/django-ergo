import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ergonaut.settings")

application = get_asgi_application()

from django.conf import settings  # noqa: E402

# The web app signs logins with SECRET_KEY: don't serve with the public default.
# (Settings only warn, so build steps like collectstatic still run without one.)
if settings.SECRET_KEY == "secret" and not settings.DEBUG and os.environ.get("ERGONAUT_ALLOW_DEFAULT_SECRET") != "1":
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("SECRET_KEY is the insecure default; set SECRET_KEY (`ergonaut up` generates one)")

# Without a Celery broker, pull the bot repo in a thread instead of beat (off
# unless ERGONAUT_BOTS_PULL_SECONDS is set).
from ergonaut.apps.bots.reloading import start_pulling  # noqa: E402

start_pulling()
