"""A bounded, read-only HTTP client for one fixed, host-configured origin."""

from __future__ import annotations

import http.client
import io
import json
import re
import time
import zlib
from dataclasses import dataclass
from itertools import chain
from urllib.parse import quote
from urllib.parse import urlencode
from urllib.parse import urlsplit

from .errors import BOUNDS_EXCEEDED
from .errors import FORBIDDEN
from .errors import INVALID_QUERY
from .errors import NOT_CONFIGURED
from .errors import NOT_FOUND
from .errors import RATE_LIMITED
from .errors import TIMEOUT
from .errors import UNAUTHORIZED
from .errors import UPSTREAM_UNAVAILABLE
from .errors import ObservabilityError

MAX_BODY_BYTES = 4 * 1024 * 1024
ERROR_TEXT_MAX = 300
MAX_JSON_DEPTH = 100
_SURROGATE = re.compile(r"[\ud800-\udfff]")


def _load_json(body: bytes):
    """Decode upstream JSON and reject excessive nesting and invalid Unicode."""
    data = json.loads(body)
    stack = [iter((data,))]
    while stack:
        try:
            value = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        if isinstance(value, str):
            if _SURROGATE.search(value):
                raise ValueError("invalid Unicode scalar")
        elif isinstance(value, list | dict):
            if len(stack) > MAX_JSON_DEPTH:
                raise ValueError("JSON nesting too deep")
            children = (
                chain.from_iterable(value.items()) if isinstance(value, dict) else value
            )
            stack.append(iter(children))
    return data


@dataclass(frozen=True)
class Limits:
    connect_timeout: float = 3.0
    read_timeout: float = 15.0
    total_timeout: float = 20.0
    max_body_bytes: int = MAX_BODY_BYTES


@dataclass
class Response:
    status: int
    content_type: str
    body: bytes


_UNSAFE_PATH = re.compile(r"(^|/)\.\.?(/|$)|//|[\\\x00-\x1f?#]")


class _DeadlineReader(io.RawIOBase):
    """Recalculate the socket timeout before every underlying network read."""

    def __init__(self, sock, remaining):
        super().__init__()
        self._sock = sock
        self._raw = sock.makefile("rb", buffering=0)
        self._remaining = remaining

    def readable(self):
        return True

    def readinto(self, buffer):
        self._sock.settimeout(self._remaining())
        return self._raw.readinto(buffer)

    def close(self):
        try:
            self._raw.close()
        finally:
            super().close()


class _DeadlineSocket:
    """Supply HTTPResponse with a buffered reader that enforces the deadline."""

    def __init__(self, sock, remaining):
        self._sock = sock
        self._remaining = remaining

    def makefile(self, mode):
        assert mode == "rb"
        return io.BufferedReader(_DeadlineReader(self._sock, self._remaining))


class BoundedClient:
    """GET-only client bound to one origin.

    * The origin comes from host configuration; ``get`` accepts only a path matching
      ``allowed`` (compiled regexes), never a URL.
    * Redirects are never followed, so credentials cannot leave the origin.
    * The token is sent only to the configured origin, as a Bearer header.
    * The body is read in chunks and capped *after* decompression.
    """

    def __init__(
        self,
        base_url: str,
        *,
        allowed: list[re.Pattern],
        token: str | None = None,
        limits: Limits | None = None,
        clock=time.monotonic,
    ):
        parts = urlsplit(base_url or "")
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ObservabilityError(
                NOT_CONFIGURED, "base URL is missing or not http(s)"
            )
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ObservabilityError(
                NOT_CONFIGURED, "base URL must not carry credentials or a query"
            )
        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.prefix = parts.path.rstrip("/")
        self.allowed = allowed
        self.token = token or None
        self.limits = limits or Limits()
        self.clock = clock

    def sanitize(self, text: str, limit: int = ERROR_TEXT_MAX) -> str:
        """Redact the configured credential before bounding upstream text."""
        if self.token:
            text = text.replace(self.token, "[redacted]")
        return text[:limit]

    # -- public -----------------------------------------------------------------

    def get_json(self, path: str, params: dict | None = None):
        response = self.get(path, params)
        try:
            return _load_json(response.body)
        except (ValueError, RecursionError):
            raise ObservabilityError(
                UPSTREAM_UNAVAILABLE,
                f"upstream returned a non-JSON response (HTTP {response.status}, "
                f"content-type {self.sanitize(response.content_type) or 'unknown'})",
            ) from None

    def get(self, path: str, params: dict | None = None) -> Response:
        """GET an allowlisted path; returns only 2xx responses."""
        self._check_path(path)
        target = self.prefix + path
        if params:
            target += "?" + urlencode(params, quote_via=quote)
        response = self._request(target)
        if response.status >= 300:  # noqa: PLR2004
            raise self._error_for(response)
        return response

    # -- internals --------------------------------------------------------------

    def _check_path(self, path: str) -> None:
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or _UNSAFE_PATH.search(path)
            or not any(rule.fullmatch(path) for rule in self.allowed)
        ):
            raise ObservabilityError(
                INVALID_QUERY, "path is not an allowed read endpoint"
            )

    def _request(self, target: str) -> Response:
        deadline = self.clock() + self.limits.total_timeout
        conn_class = (
            http.client.HTTPSConnection
            if self.scheme == "https"
            else http.client.HTTPConnection
        )
        connect_timeout = self._remaining(deadline, self.limits.connect_timeout)
        conn = conn_class(self.host, self.port, timeout=connect_timeout)
        conn.response_class = lambda sock, **kwargs: http.client.HTTPResponse(
            _DeadlineSocket(sock, lambda: self._remaining(deadline)), **kwargs
        )
        headers = {"Accept": "application/json, image/png", "Accept-Encoding": "gzip"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        raw = None
        try:
            conn.connect()
            sock = conn.sock
            sock.settimeout(self._remaining(deadline))
            conn.request("GET", target, headers=headers)
            raw = conn.getresponse()
            body = self._read_body(raw, sock, deadline)
            self._remaining(deadline)
            return Response(raw.status, raw.getheader("Content-Type", "") or "", body)
        except ObservabilityError:
            raise
        except TimeoutError:
            raise ObservabilityError(
                TIMEOUT, "upstream did not answer in time"
            ) from None
        except (OSError, http.client.HTTPException, zlib.error):
            raise ObservabilityError(
                UPSTREAM_UNAVAILABLE, "upstream is unreachable or sent a bad response"
            ) from None
        finally:
            if raw is not None:
                raw.close()
            conn.close()

    def _remaining(self, deadline: float, timeout: float | None = None) -> float:
        left = deadline - self.clock()
        if left <= 0:
            raise ObservabilityError(TIMEOUT, "overall request deadline exceeded")
        return min(self.limits.read_timeout if timeout is None else timeout, left)

    def _read_body(self, raw: http.client.HTTPResponse, sock, deadline: float) -> bytes:
        cap = self.limits.max_body_bytes
        encoding_header = raw.getheader("Content-Encoding") or "identity"
        encoding = encoding_header.lower()
        if encoding not in {"identity", "gzip"}:
            raise ObservabilityError(
                UPSTREAM_UNAVAILABLE,
                f"unsupported content-encoding {self.sanitize(encoding_header)!r}",
            )
        inflater = (
            zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
        )
        out = bytearray()
        too_big = ObservabilityError(
            BOUNDS_EXCEEDED, f"response exceeds the {cap} byte limit; narrow the query"
        )
        while not raw.isclosed():
            sock.settimeout(self._remaining(deadline))
            chunk = raw.read(16384)
            if not chunk:
                break
            if inflater:
                data = inflater.decompress(chunk, cap - len(out) + 1)
                while True:
                    out += data
                    if len(out) > cap:
                        raise too_big
                    if not inflater.unconsumed_tail:
                        break
                    data = inflater.decompress(
                        inflater.unconsumed_tail, cap - len(out) + 1
                    )
            else:
                out += chunk
                if len(out) > cap:
                    raise too_big
        if inflater:
            out += inflater.flush()
            if len(out) > cap:
                raise too_big
        return bytes(out)

    def _error_for(self, response: Response) -> ObservabilityError:
        status = response.status
        detail = self._detail(response)
        suffix = f": {detail}" if detail else ""
        if status == 401:  # noqa: PLR2004
            return ObservabilityError(
                UNAUTHORIZED, "credential missing, malformed or expired (HTTP 401)"
            )
        if status == 403:  # noqa: PLR2004
            return ObservabilityError(
                FORBIDDEN, "credential is valid but not permitted (HTTP 403)"
            )
        if status == 404:  # noqa: PLR2004
            return ObservabilityError(NOT_FOUND, f"not found (HTTP 404){suffix}")
        if status == 429:  # noqa: PLR2004
            return ObservabilityError(
                RATE_LIMITED, "upstream is rate limiting; retry later (HTTP 429)"
            )
        if status in {408, 504}:
            return ObservabilityError(
                TIMEOUT, f"upstream timed out (HTTP {status}){suffix}"
            )
        if status in {400, 422}:
            return ObservabilityError(
                INVALID_QUERY, f"upstream rejected the request (HTTP {status}){suffix}"
            )
        if 300 <= status < 400:  # noqa: PLR2004
            return ObservabilityError(
                UPSTREAM_UNAVAILABLE,
                f"upstream redirected (HTTP {status}); redirects are not followed",
            )
        if status >= 500:  # noqa: PLR2004
            return ObservabilityError(
                UPSTREAM_UNAVAILABLE, f"upstream error (HTTP {status})"
            )
        return ObservabilityError(
            INVALID_QUERY, f"upstream rejected the request (HTTP {status})"
        )

    def _detail(self, response: Response) -> str:
        """Prometheus/Grafana JSON error text only; HTML and other bodies are dropped."""
        if "json" not in response.content_type.lower():
            return ""
        try:
            data = _load_json(response.body)
        except (ValueError, RecursionError):
            return ""
        if isinstance(data, dict):
            for key in ("error", "message"):
                if isinstance(data.get(key), str):
                    return " ".join(self.sanitize(data[key]).split())
        return ""
