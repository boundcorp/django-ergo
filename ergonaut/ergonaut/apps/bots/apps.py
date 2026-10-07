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
            if sender is ergo_models.ThreadMessage:
                notify(instance.recipient_session_id)
                notify(instance.sender_session_id)
                return
            session_id = getattr(instance, "session_id", None)
            if session_id is None and sender is ergo_models.MessageBlock:
                session_id = getattr(instance.message, "session_id", None)
            notify(session_id)

        for model in (
            ergo_models.SessionMessage,
            ergo_models.MessageBlock,
            ergo_models.StructuredCall,
            ergo_models.ConversationAttachment,
            ergo_models.ThreadMessage,
        ):
            post_save.connect(changed, sender=model, dispatch_uid=f"ergonaut-live-{model.__name__}")

        # What people do on bot pages: kept, and shown to the bot on its next turn.
        from django_ergo.bots import page_actions

        from ergonaut.apps.bots.pageactions import DjangoCallLog

        page_actions.set_call_log(DjangoCallLog())

        # Pages re-render when a table they read changes: tell the open streams.
        from django_ergo.bots.tables import table_changed

        def publish_table(sender, bot_name, table, **kwargs):
            from ergonaut.apps.bots.tasks import notify_table

            notify_table(bot_name, table)

        table_changed.connect(publish_table, dispatch_uid="ergonaut-live-tables")
