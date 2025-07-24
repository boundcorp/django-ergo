from django.apps import AppConfig


class ErgoConfig(AppConfig):
    name = "papa.apps.ergo"
    verbose_name = "Ergo Agent System"

    def ready(self):
        # Import signal handlers and any initialization code here
        pass
