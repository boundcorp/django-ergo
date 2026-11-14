"""Typed, sanitized errors for the observability library."""

from __future__ import annotations

NOT_CONFIGURED = "not_configured"
UNAUTHORIZED = "unauthorized"
FORBIDDEN = "forbidden"
NOT_FOUND = "not_found"
INVALID_QUERY = "invalid_query"
RATE_LIMITED = "rate_limited"
UPSTREAM_UNAVAILABLE = "upstream_unavailable"
TIMEOUT = "timeout"
BOUNDS_EXCEEDED = "bounds_exceeded"

CODES = frozenset(
    {
        NOT_CONFIGURED,
        UNAUTHORIZED,
        FORBIDDEN,
        NOT_FOUND,
        INVALID_QUERY,
        RATE_LIMITED,
        UPSTREAM_UNAVAILABLE,
        TIMEOUT,
        BOUNDS_EXCEEDED,
    }
)


class ObservabilityError(Exception):
    """A failure whose ``code`` is one of CODES and whose message is safe to show a model.

    Messages never carry upstream HTML, auth headers or tokens.
    """

    def __init__(self, code: str, message: str, *, minimum_step: int | None = None):
        if code not in CODES:
            msg = f"unknown observability error code {code!r}"
            raise ValueError(msg)
        super().__init__(message)
        self.code = code
        self.message = message
        self.minimum_step = minimum_step

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"
