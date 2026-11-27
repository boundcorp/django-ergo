"""Protected environment variables for subprocesses and tools."""

import os

DEFAULT_PROTECTED_ENV_PREFIXES = ("FIREWORKS_",)


def protected_env_prefixes() -> tuple[str, ...]:
    """Return protected prefixes, including optional host-configured additions."""
    extra: list = []
    try:
        from django_ergo.settings import api_settings

        extra = list(getattr(api_settings, "PROTECTED_ENV_PREFIXES", None) or [])
    except Exception:  # noqa: BLE001 - settings not configured: defaults still apply
        extra = []
    return tuple(
        dict.fromkeys(
            [*DEFAULT_PROTECTED_ENV_PREFIXES, *(str(p).upper() for p in extra if p)]
        )
    )


def is_protected_env(name) -> bool:
    return str(name or "").upper().startswith(protected_env_prefixes())


# Native subscription login/config survives ordinary provider scrubbing.
NATIVE_ENV = frozenset(
    {"CLAUDE_CODE_OAUTH_TOKEN", "CODEX_HOME", "HOME", "CLAUDE_CONFIG_DIR"}
)
SAFE_ENV = NATIVE_ENV | frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "TERM",
        "SHELL",
        "SYSTEMROOT",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "DISABLE_AUTOUPDATER",
        "DISABLE_TELEMETRY",
        "DISABLE_ERROR_REPORTING",
        "ENABLE_TOOL_SEARCH",
        "CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH",
        "DISABLE_AUTO_COMPACT",
        "DISABLE_COMPACT",
        "CLAUDE_CODE_TOTAL_TOKENS_REMINDER",
    }
)
PROVIDER_PARTS = frozenset(
    {
        "OPENAI",
        "ANTHROPIC",
        "GEMINI",
        "GOOGLE",
        "GCP",
        "GCPJSON",
        "XAI",
        "FIREWORKS",
        "AZURE",
        "OPENROUTER",
        "MINIMAX",
        "AWS",
        "CLOUDSDK",
    }
)
CREDENTIAL_SUFFIXES = (
    "_API_KEY",
    "_AUTH_TOKEN",
    "_ACCESS_TOKEN",
    "_BASE_URL",
    "_CREDENTIALS",
    "_CREDENTIALS_JSON",
    "_SERVICE_ACCOUNT_JSON",
    "_CREDENTIALS_FILE",
    "_TOKEN_FILE",
    "_PRIVATE_KEY",
    "_PRIVATE_KEY_FILE",
)


def is_provider_credential(name, credentials=None) -> bool:
    """Recognize aliases, vendor families, prefixed keys, JSON and file pointers."""
    from django_ergo.provider_credentials import provider_credential_names

    upper = str(name).upper()
    credentials = provider_credential_names() if credentials is None else credentials
    # Explicit aliases and private prefixes override even native keep names.
    if upper in credentials or is_protected_env(upper):
        return True
    if upper in NATIVE_ENV:
        return False
    return (
        bool(PROVIDER_PARTS.intersection(upper.split("_")))
        or upper.endswith(CREDENTIAL_SUFFIXES)
        or upper.startswith("CLAUDE_CODE_USE_")
    )


def scrubbed_env(env=None) -> dict[str, str]:
    """Remove credentials; discovery errors allow only known safe native fields.

    This is accidental-inheritance hygiene, not a same-UID security sandbox.
    """
    source = os.environ if env is None else env
    from django_ergo.provider_credentials import credential_discovery_incomplete
    from django_ergo.provider_credentials import provider_credential_names

    credentials = provider_credential_names()
    incomplete = credential_discovery_incomplete()
    return {
        k: v
        for k, v in source.items()
        if (not incomplete or str(k).upper() in SAFE_ENV)
        and not is_provider_credential(k, credentials)
    }
