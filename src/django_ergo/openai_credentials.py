"""Explicit, fail-closed selection of OpenAI credentials by consumer."""

import os


def selected_openai_key(name: str = "") -> str:
    """Use the legacy SDK name only when no explicit name was configured."""
    from django_ergo.openai_env import register_openai_alias

    register_openai_alias(name)
    source = name or "OPENAI_API_KEY"
    key = os.environ.get(source, "")
    if not key:
        msg = f"OpenAI API key is required: environment variable {source} is not set"
        raise ValueError(msg)
    return key


def voice_openai_key() -> str:
    from django_ergo.settings import api_settings

    return selected_openai_key(api_settings.VOICE_OPENAI_API_KEY_ENV)
