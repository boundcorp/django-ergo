# ruff: noqa: TRY003, EM101
"""Strict, content-free validation for the opt-in analysis configuration."""

from django.core.exceptions import ImproperlyConfigured


def enabled_value(value):
    if type(value) is bool:
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off", ""}:
            return False
    raise ImproperlyConfigured("ERGO_ANALYSIS_ENABLED must be a boolean")


def retention_days(value):
    if type(value) is int and 1 <= value <= 365:  # noqa: PLR2004
        return value
    if (
        isinstance(value, str)
        and len(value) <= 3  # noqa: PLR2004
        and value.isascii()
        and value.isdecimal()
        and 1 <= int(value) <= 365  # noqa: PLR2004
    ):
        return int(value)
    raise ImproperlyConfigured(
        "ERGO_ANALYSIS_RETENTION_DAYS must be an integer from 1 to 365"
    )


def controller_key(value):
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        try:
            return value.encode("utf-8")
        except UnicodeError:
            pass
    raise ImproperlyConfigured(
        "ERGO_ANALYSIS_CONTROLLER_KEY must be UTF-8 text or bytes"
    ) from None
