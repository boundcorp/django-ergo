"""
Multi-engine conversation framework for django-ergo.

Provides lossless, engine-native conversation storage and management
for Claude (CLI + API) and OpenAI engines.

Models are imported lazily - use:
    from django_ergo.conversation.models import ConversationSession
"""

from django_ergo.conversation.runtime import EngineSpec
from django_ergo.conversation.runtime import build_engine
from django_ergo.conversation.runtime import generate_once
from django_ergo.conversation.runtime import get_default_engine_spec
from django_ergo.conversation.runtime import run_workflow_task

__all__ = [
    "EngineSpec",
    "build_engine",
    "generate_once",
    "get_default_engine_spec",
    "run_workflow_task",
]
