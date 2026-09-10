"""Provider-free tests: no dotenv, shared service, filesystem KB or legacy models."""

SECRET_KEY = "knowledge-test-only"
INSTALLED_APPS = ["django_ergo.knowledge"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
