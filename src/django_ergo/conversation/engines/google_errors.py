"""Typed errors of the Google Gemini engine.

The structured-call runner classifies failures by exception class *name*
(``structured._ERROR_CATEGORIES`` and ``_TRANSIENT_ERRORS``) so no SDK has to
be importable. The classes here reuse those names on purpose, so a Gemini
failure is categorized and retried exactly like the same failure from the
OpenAI or Anthropic SDK:

========================  =====================================  ============
class                     meaning                                 category
========================  =====================================  ============
``AuthenticationError``   key missing, invalid or not permitted   ``auth``
``QuotaExhaustedError``   429 for a daily/billing/spend quota     ``network``
``RateLimitError``        429 for a per-minute rate               ``network``
``BadRequestError``       400/404/422: the request is wrong       ``model``
``OverloadedError``       503 UNAVAILABLE                         ``model``, retried
``InternalServerError``   500/504, malformed function call        ``model``, retried
``APITimeoutError``       the request timed out                   ``network``, retried
``APIConnectionError``    the network failed                      ``network``, retried
========================  =====================================  ============

Messages never contain the API key. A message that names a limit includes the
status code (``429``) so ``bots.routing.is_limit_error`` offers another model.
"""

from __future__ import annotations


class GoogleAPIError(RuntimeError):
    """A failed Gemini request. ``status`` is the HTTP status (0 when none),
    ``reason`` Google's error status (``RESOURCE_EXHAUSTED``) and
    ``retry_after`` the seconds Google asked to wait, when it said."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        reason: str = "",
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.retry_after = retry_after


class AuthenticationError(GoogleAPIError):
    """No key, a key Google rejects, or one that may not use the model."""


class RateLimitError(GoogleAPIError):
    """429 for a rate that recovers within a minute or so (``retry_after``)."""

    exhausted = False


class QuotaExhaustedError(RateLimitError):
    """429 for a quota that doesn't recover soon: a daily limit, a spend cap,
    or billing. Routing should move to another model rather than wait."""

    exhausted = True


class BadRequestError(GoogleAPIError):
    """The request was refused as invalid (400, 404 unknown model, 422)."""


class InternalServerError(GoogleAPIError):
    """Google failed (500/504) or returned an unusable function call."""


class OverloadedError(GoogleAPIError):
    """503 UNAVAILABLE: the model is overloaded; try again shortly."""


class APITimeoutError(GoogleAPIError):
    """The request didn't finish in time."""


class APIConnectionError(GoogleAPIError):
    """The request never reached Google (DNS, TLS, reset)."""


class EmptyResponseError(InternalServerError):
    """Google answered with no candidate and no block reason."""
