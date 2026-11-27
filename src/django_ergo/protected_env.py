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


def scrubbed_env(env=None) -> dict[str, str]:
    """Return an environment copy without protected variables."""
    source = os.environ if env is None else env
    from django_ergo.provider_credentials import provider_credential_names

    prefixes = protected_env_prefixes()
    credentials = provider_credential_names()
    return {
        k: v
        for k, v in source.items()
        if not str(k).upper().startswith(prefixes)
        and str(k).upper() not in credentials
        and not str(k).upper().startswith("CLAUDE_CODE_USE_")
    }
