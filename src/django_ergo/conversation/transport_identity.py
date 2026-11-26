"""Non-secret identity shared by routed guards and transport constructors."""

import json
import os
from urllib.parse import urlsplit

from django_ergo.conversation.inference_policy import effective_anthropic_base_url
from django_ergo.conversation.inference_policy import effective_openai_base_url

KEY_ENVS = {
    "openai": ["OPENAI_API_KEY"],
    "claude": ["ANTHROPIC_API_KEY"],
    "google": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
}


def identity(kind, transport, config, *, key_env="", account="", model=""):  # noqa: PLR0913
    """Never inspect, hash, log or return a secret value."""
    endpoint = (
        config.get("base_url") or config.get("api_base") or config.get("endpoint") or ""
    )
    if kind == "openai":
        endpoint = effective_openai_base_url(endpoint)
    elif kind == "claude":
        endpoint = effective_anthropic_base_url(endpoint)
    elif kind == "google":
        endpoint = endpoint or "https://generativelanguage.googleapis.com/v1beta"
    parsed = urlsplit(str(endpoint))
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        msg = "Guarded endpoint must not contain URL credentials, query or fragment"
        raise ValueError(msg)
    source = key_env or config.get("api_key_env")
    if not source:
        defaults = KEY_ENVS.get(kind, [])
        source = next(
            (name for name in defaults if os.environ.get(name)),
            defaults[0] if defaults else "",
        )
    if config.get("api_key") and not (key_env or config.get("api_key_env")):
        source = "unidentified-inline-credential"
    return json.dumps(
        [kind, transport, source, account, str(endpoint).rstrip("/"), model],
        separators=(",", ":"),
    )


def with_endpoint(captured, endpoint):
    """An SDK client's current destination must match its captured identity."""
    parts = json.loads(captured)
    parts[4] = str(endpoint).rstrip("/")
    return json.dumps(parts, separators=(",", ":"))
