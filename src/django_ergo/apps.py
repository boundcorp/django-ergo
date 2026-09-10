from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured


class DjangoErgoConfig(AppConfig):
    name = "django_ergo"
    verbose_name = "django-ergo"
    default_auto_field = "django.db.models.AutoField"

    def import_models(self):
        try:
            super().import_models()
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.split(".")[0] in {
                "pgvector",
                "psycopg",
                "psycopg2",
            }:
                message = "The legacy django_ergo app requires django-ergo[legacy] and PostgreSQL with vector. The standalone django_ergo.knowledge app does not."
                raise ImproperlyConfigured(message) from exc
            raise

    def ready(self):
        import django_ergo.conversation.models  # noqa: F401
