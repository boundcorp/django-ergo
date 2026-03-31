"""
Multi-engine conversation framework for django-ergo.

Provides lossless, engine-native conversation storage and management
for Claude (CLI + API) and OpenAI engines.

Models are imported lazily - use:
    from django_ergo.conversation.models import ConversationSession
"""
