from django.apps import AppConfig


class DjangoErgoConfig(AppConfig):
    name = "django_ergo"
    verbose_name = "django-ergo"
    default_auto_field = "django.db.models.AutoField"

    def ready(self):
        import django_ergo.conversation.models  # noqa: F401
