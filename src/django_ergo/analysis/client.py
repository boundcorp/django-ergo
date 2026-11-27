# ruff: noqa: TRY003, EM101, PLR2004
"""Application-side HTTP broker client. No process or runtime access."""

import ssl
from urllib.parse import urlparse

from django_ergo.analysis.config import controller_key
from django_ergo.analysis.protocol import MAX_RESPONSE
from django_ergo.analysis.protocol import ProtocolError
from django_ergo.analysis.protocol import open_envelope
from django_ergo.analysis.protocol import seal
from django_ergo.analysis.runner import RunnerUnavailableError


class ControllerClient:
    def __init__(self, url, key, *, ca_file=None, transport=None):
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise RunnerUnavailableError(
                "Controller requires an authenticated HTTPS endpoint"
            )
        if not isinstance(key, bytes) or len(key) < 32:
            raise RunnerUnavailableError(
                "Controller authentication key is not configured"
            )
        self.verify = True
        if ca_file:
            try:
                self.verify = ssl.create_default_context(cafile=ca_file)
            except (OSError, ValueError, TypeError):
                raise RunnerUnavailableError(
                    "Cannot load ERGO_ANALYSIS_CONTROLLER_CA_FILE; mount a readable PEM CA bundle"
                ) from None
        self.url = url.rstrip("/") + "/v1"
        self.key = key
        self.transport = transport

    def call(self, body):
        import httpx

        raw = seal(self.key, body)
        nonce = open_envelope(self.key, raw)["nonce"]
        try:
            with httpx.Client(  # noqa: SIM117 — stream lifetime contained by client
                timeout=15,
                verify=self.verify,
                trust_env=False,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                with client.stream(
                    "POST",
                    self.url,
                    content=raw,
                    headers={"Content-Type": "application/json"},
                ) as response:
                    if response.status_code != 200:
                        raise RunnerUnavailableError(
                            "Controller rejected request or is unavailable"
                        )
                    data = bytearray()
                    for chunk in response.iter_bytes(chunk_size=8192):
                        if len(data) + len(chunk) > MAX_RESPONSE:
                            raise ProtocolError("Controller response exceeds byte cap")
                        data.extend(chunk)
            message = open_envelope(self.key, data)
            value = message["body"]
            if (
                not isinstance(value, dict)
                or set(value) != {"request_nonce", "response"}
                or value["request_nonce"] != nonce
                or not isinstance(value["response"], dict)
            ):
                raise ProtocolError("Controller response does not bind request")
            return value["response"]
        except httpx.HTTPError:
            raise RunnerUnavailableError(
                "Controller unavailable; no execution fallback"
            ) from None


def configured_client():
    from django.conf import settings

    if getattr(settings, "ERGO_ANALYSIS_ENABLED", False) is not True:
        raise RunnerUnavailableError("Isolated analysis is disabled")
    url = getattr(settings, "ERGO_ANALYSIS_CONTROLLER_URL", "")
    key = getattr(settings, "ERGO_ANALYSIS_CONTROLLER_KEY", b"")
    key = controller_key(key)
    ca_file = getattr(settings, "ERGO_ANALYSIS_CONTROLLER_CA_FILE", "")
    return ControllerClient(url, key, ca_file=ca_file)
