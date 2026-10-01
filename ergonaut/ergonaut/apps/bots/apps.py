from django.apps import AppConfig


class BotsConfig(AppConfig):
    name = "ergonaut.apps.bots"
    label = "ergonaut_bots"

    def ready(self):
        # Ergo registers its models on Django's default admin site; show them
        # in ours too.
        from django.contrib import admin

        from ergonaut.utils.admin import admin_site

        for model, model_admin in admin.site._registry.items():
            if not admin_site.is_registered(model):
                admin_site.register(model, type(model_admin))
