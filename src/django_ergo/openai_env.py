"""Names withheld from ordinary children, limited to OpenAI consumers.

Collect only explicit selectors at parsed OpenAI consumer boundaries and the
three known settings below. Never crawl plugin config, YAML files or environment
suffixes. Retain old names for this process across reloads; cache no values.
This is inheritance hygiene, not a sandbox against same-UID file/process access.
"""

import os
import re
from collections.abc import Mapping
from threading import RLock

_LOCK = RLock()
_NAMES: set[str] = set()
_DEFAULT_NAMES = frozenset({"OPENAI_API_KEY", "BOUNDCORP_ORG_OPENAI_KEY"})
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,255}\Z")


def register_openai_alias(name) -> None:
    """Record one explicit OpenAI selector, without recursive config discovery."""
    if name is None or name == "":
        return
    if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
        msg = "OpenAI api_key_env must be an environment variable name (max 256 characters)"
        raise ValueError(msg)
    with _LOCK:
        _NAMES.add(name.upper())


def default_engine_is_openai() -> bool:
    """Whether a bot that names no engine type inherits the OpenAI engine.

    Reads the raw setting (no imports), so it is safe at bot load time.
    """
    from django.conf import settings

    from django_ergo.settings import DEFAULTS

    config = {}
    if settings.configured:
        raw = getattr(settings, "DJANGO_ERGO", {})
        config = raw if isinstance(raw, Mapping) else {}
    return (
        config.get("CONVERSATION_ENGINE_TYPE", DEFAULTS["CONVERSATION_ENGINE_TYPE"])
        == "openai"
    )


def openai_credential_names() -> frozenset[str]:
    from django.conf import settings

    if settings.configured:
        # Inspect raw, known fields: accessing api_settings.EMBEDDING_PROVIDER
        # imports a class and would make an unrelated broken provider affect
        # every child launch. No imports or file reads are needed here.
        from django_ergo.settings import DEFAULTS

        config = getattr(settings, "DJANGO_ERGO", {})
        if isinstance(config, Mapping):
            register_openai_alias(config.get("VOICE_OPENAI_API_KEY_ENV"))
            if (
                config.get(
                    "CONVERSATION_ENGINE_TYPE", DEFAULTS["CONVERSATION_ENGINE_TYPE"]
                )
                == "openai"
            ):
                chat = config.get("CONVERSATION_ENGINE_CONFIG") or {}
                if isinstance(chat, Mapping):
                    register_openai_alias(chat.get("api_key_env"))
            provider = config.get("EMBEDDING_PROVIDER", DEFAULTS["EMBEDDING_PROVIDER"])
            if (
                provider == "django_ergo.embedding_providers.OpenAIEmbeddingProvider"
                or (
                    isinstance(provider, type)
                    and provider.__module__ == "django_ergo.embedding_providers"
                    and provider.__name__ == "OpenAIEmbeddingProvider"
                )
            ):
                embedding = config.get("EMBEDDING_PROVIDER_CONFIG") or {}
                if isinstance(embedding, Mapping):
                    register_openai_alias(embedding.get("api_key_env"))
    # Standalone host commands may not have initialized Django settings yet.
    register_openai_alias(os.environ.get("VOICE_OPENAI_API_KEY_ENV"))
    with _LOCK:
        return _DEFAULT_NAMES | frozenset(_NAMES)
