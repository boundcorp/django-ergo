import logging
import os
import sys

if "TEST_USE_ENV" not in os.environ:
    del sys.modules["ergonaut.settings"]
    del sys.modules["ergonaut.settings.project"]

from ergonaut.settings import *

SECRET_KEY = "test-only-key-at-least-32-characters-long"
IS_TEST = True

logging.disable(logging.CRITICAL)
DEBUG = True

PASSWORD_HASHERS = ("django.contrib.auth.hashers.MD5PasswordHasher",)
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
STORAGES = {
    **STORAGES,
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}
BASE_URL = f"http://localhost:{BACKEND_PORT}"

# pgserver handles the database automatically — no override needed.
# Tests use the same embedded postgres as dev, with a test-specific database.

HAS_DOCKER_SERVICES = all(os.environ.get(v) for v in ("DATABASE_URL", "CELERY_BROKER_URL", "S3_ENDPOINT_URL"))

# Test bots start without Ergo's default skills (bot_management would need a
# git checkout); tests that want them set DEFAULT_SKILLS themselves.
DJANGO_ERGO = {**DJANGO_ERGO, "DEFAULT_SKILLS": []}  # noqa: F405
