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

        # The bots load on first use and reload when their files change.
        from django_ergo.bots import webhooks

        from ergonaut.apps.bots.reloading import ReloadingRegistry

        webhooks.set_registry(ReloadingRegistry())

        # Wake live SSE streams whenever a session's messages or calls change.
        from django.db.models.signals import post_save
        from django_ergo.conversation import models as ergo_models

        from ergonaut.apps.bots.tasks import notify

        def changed(sender, instance, **kwargs):
            session_id = getattr(instance, "session_id", None)
            if session_id is None and sender is ergo_models.ClaudeContentBlock:
                session_id = getattr(instance.message, "session_id", None)
            notify(session_id)

        for model in (
            ergo_models.ClaudeMessage,
            ergo_models.ClaudeContentBlock,
            ergo_models.OpenAIMessage,
            ergo_models.StructuredCall,
            ergo_models.ConversationAttachment,
        ):
            post_save.connect(changed, sender=model, dispatch_uid=f"ergonaut-live-{model.__name__}")
