"""Runpod REST operations for a supervised experiment, not launch authorization.

Only the fixed public REST endpoint is supported. Credentials are obtained from
an injected callback and are never included in errors. A timeout is not proof
that a mutating request failed; callers must reconcile rather than replay it.
"""

from __future__ import annotations

import ipaddress
import json
import math
import time
from dataclasses import dataclass
from http import HTTPStatus
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler
from urllib.request import Request
from urllib.request import build_opener

from .ssh import public_key

_BASE = "https://rest.runpod.io/v1"
_MAX_RESPONSE = 2 * 1024 * 1024
_MAX_TIMEOUT = 30
_MAX_SSE_LINE = 65536
_MAX_PORT = 65535
_NOT_FOUND = object()


class ProviderError(Exception):
    """A deliberately sanitized provider or credential failure."""


class OwnershipError(ProviderError):
    """Provider response does not prove the expected ownership and profile."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: PLR0913
        return None


@dataclass(frozen=True)
class Ownership:
    account: str
    attempt: str
    fingerprint: str
    image: str
    container_disk_gb: int

    def __post_init__(self):
        if (
            not all(
                isinstance(s, str) and s
                for s in (self.account, self.attempt, self.fingerprint, self.image)
            )
            or type(self.container_disk_gb) is not int
            or self.container_disk_gb <= 0
        ):
            msg = "invalid ownership profile"
            raise ValueError(msg)

    def matches(self, pod, *, request_bound=False):
        """Bind account and attempt, never name; missing storage needs request proof."""
        if not isinstance(pod, dict) or not isinstance(pod.get("env"), dict):
            return False
        env = pod["env"]
        return (
            pod.get("consumerUserId") == self.account
            and env.get("ERGO_ATTEMPT_ID") == self.attempt
            and env.get("ERGO_ATTEMPT_FINGERPRINT") == self.fingerprint
            and pod.get("image") == self.image
            and type(pod.get("containerDiskInGb")) is int
            and pod["containerDiskInGb"] == self.container_disk_gb
            and type(pod.get("volumeInGb")) is int
            and pod["volumeInGb"] == 0
            and (request_bound or "networkVolume" in pod)
            and pod.get("networkVolume") is None
        )


def _safe_pod(pod):
    """Keep only cleanup fields, never other environment variables."""
    fields = (
        "id",
        "consumerUserId",
        "containerDiskInGb",
        "volumeInGb",
        "networkVolume",
        "costPerHr",
    )
    safe = {key: pod[key] for key in fields if key in pod}
    if "imageName" in pod:
        safe["image"] = pod["imageName"]
    if isinstance(pod.get("env"), dict):
        safe["env"] = {
            key: pod["env"][key]
            for key in ("ERGO_ATTEMPT_ID", "ERGO_ATTEMPT_FINGERPRINT")
            if key in pod["env"]
        }
    if safe.get("networkVolume") is not None:
        safe["networkVolume"] = True
    return safe


class RunpodREST:
    """Small REST client; the credential callback returns a bearer token string."""

    def __init__(self, credential, *, timeout=10, opener=None):
        if not callable(credential):
            msg = "credential callback required"
            raise TypeError(msg)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not math.isfinite(timeout)
            or not 0 < timeout <= _MAX_TIMEOUT
        ):
            msg = "timeout must be between zero and 30 seconds"
            raise ValueError(msg)
        self._credential = credential
        self._timeout = timeout
        self._opener = opener if opener is not None else build_opener(_NoRedirect())

    def _token(self):
        token = self._credential()
        if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
            msg = "invalid provider credential"
            raise ValueError(msg)
        return token

    def _request(self, method, path, *, body=None, absent_ok=False):
        try:
            token = self._token()
            data = None if body is None else json.dumps(body, allow_nan=False).encode()
            request = Request(  # noqa: S310 -- fixed HTTPS origin
                _BASE + path,
                data=data,
                method=method,
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                    "User-Agent": "Ergonaut-native-preflight",
                },
            )
            with self._opener.open(request, timeout=self._timeout) as response:
                payload = response.read(_MAX_RESPONSE + 1)
            if len(payload) > _MAX_RESPONSE:
                raise ValueError  # noqa: TRY301
            return json.loads(payload) if payload else None
        except HTTPError as exc:
            if absent_ok and exc.code == HTTPStatus.NOT_FOUND:
                return _NOT_FOUND
            msg = "provider HTTP request failed"
            raise ProviderError(msg) from None
        except Exception:  # noqa: BLE001 -- credential and HTTP errors may contain secrets
            msg = "provider request failed"
            raise ProviderError(msg) from None

    def list_pods(self):
        result = self._request("GET", "/pods?includeNetworkVolume=true")
        if not isinstance(result, list) or any(not isinstance(p, dict) for p in result):
            msg = "invalid provider response"
            raise ProviderError(msg)
        return [_safe_pod(pod) for pod in result]

    def get_pod(self, pod_id):
        if not isinstance(pod_id, str) or not pod_id or not pod_id.isalnum():
            msg = "invalid Pod ID"
            raise ValueError(msg)
        result = self._request(
            "GET",
            "/pods/" + quote(pod_id, safe="") + "?includeNetworkVolume=true",
            absent_ok=True,
        )
        if result is _NOT_FOUND:
            return None
        if not isinstance(result, dict) or result.get("id") != pod_id:
            msg = "invalid provider response"
            raise ProviderError(msg)
        return _safe_pod(result)

    def ssh_connection(self, pod_id, ownership, *, deadline):  # noqa: C901 -- bounded ownership/log/address handshake
        """Read an owned direct address and authenticate its host key via logs.

        This performs GETs only; it neither starts SSH nor authorizes work.
        The caller writes ``known_hosts`` and uses ``HostKeyAlias`` with strict
        checking. A missing marker, mapping, access permission or log API fails
        closed. Never substitute ssh-keyscan or accept-new.
        """
        if (
            not isinstance(pod_id, str)
            or not pod_id.isascii()
            or not pod_id.isalnum()
            or not isinstance(ownership, Ownership)
            or not isinstance(deadline, int | float)
            or not math.isfinite(deadline)
            or deadline <= time.time()
        ):
            msg = "owned Pod and future connection deadline required"
            raise ValueError(msg)
        path = "/pods/" + quote(pod_id, safe="") + "?includeNetworkVolume=true"
        pod = self._request("GET", path)
        if (
            not isinstance(pod, dict)
            or pod.get("id") != pod_id
            or not ownership.matches(_safe_pod(pod), request_bound=True)
        ):
            msg = "SSH rental ownership unverified"
            raise OwnershipError(msg)
        try:
            address = str(ipaddress.ip_address(pod["publicIp"]))
            port = pod["portMappings"]["22"]
            if type(port) is not int or not 1 <= port <= _MAX_PORT:
                raise ValueError  # noqa: TRY301
            request = Request(
                "https://api.runpod.io/v2/pods/"
                + quote(pod_id, safe="")
                + "/logs?source=container&tail=5000",
                method="GET",
                headers={
                    "Authorization": "Bearer " + self._token(),
                    "Accept": "text/event-stream",
                    "User-Agent": "Ergonaut-native-preflight",
                },
            )
            prefix = "ERGO_SSH_HOST_KEY " + ownership.fingerprint + " "
            size = 0
            key = None
            with self._opener.open(
                request, timeout=min(self._timeout, deadline - time.time())
            ) as response:
                while time.time() < deadline:
                    line = response.readline(_MAX_SSE_LINE + 1)
                    size += len(line)
                    if not line or len(line) > _MAX_SSE_LINE or size > _MAX_RESPONSE:
                        break
                    if not line.startswith(b"data:"):
                        continue
                    event = json.loads(line[5:])
                    text = event.get("line", "")
                    if (
                        event.get("source") == "container"
                        and isinstance(text, str)
                        and text.startswith(prefix)
                    ):
                        key = public_key(text[len(prefix) :])
                        break
            if key is None or time.time() >= deadline:
                raise ValueError  # noqa: TRY301
            # An address change while authenticating is not a usable binding.
            current = self._request("GET", path)
            if (
                not isinstance(current, dict)
                or current.get("id") != pod_id
                or not ownership.matches(_safe_pod(current), request_bound=True)
                or current.get("publicIp") != address
                or current.get("portMappings", {}).get("22") != port
                or time.time() >= deadline
            ):
                raise ValueError  # noqa: TRY301
            alias = "ergo-" + ownership.fingerprint
            return {
                "host": address,
                "port": port,
                "host_key_alias": alias,
                "known_hosts": alias + " " + key + "\n",
            }
        except Exception:  # noqa: BLE001 -- provider errors may contain credentials
            msg = "authenticated SSH connection unverified"
            raise ProviderError(msg) from None

    def create_pod(self, body):
        result = self._request("POST", "/pods", body=body)
        if not isinstance(result, dict):
            msg = "invalid provider create response"
            raise ProviderError(msg)
        return _safe_pod(result)

    def terminate_owned(self, pod_id, ownership, *, request_bound=False):
        """Re-read exact ownership before DELETE; caller must separately read back.

        A successful delete request is not proof of termination or storage removal.
        An unexpected attached volume prevents deletion rather than risking data.
        """
        if not isinstance(ownership, Ownership):
            msg = "ownership profile required"
            raise TypeError(msg)
        pod = self.get_pod(pod_id)
        if pod is None or not ownership.matches(pod, request_bound=request_bound):
            msg = "ownership or storage profile unverified"
            raise OwnershipError(msg)
        self._request("DELETE", "/pods/" + quote(pod_id, safe=""))


class FixtureRunpodREST(RunpodREST):
    """Exercise POST serialization with a supplied offline opener, never credentials.

    This is a fixture harness, not a sandbox for untrusted openers. Callers must
    supply an in-memory fake.
    """

    def __init__(self, opener):
        if opener is None:
            msg = "offline fixture opener required"
            raise ValueError(msg)
        super().__init__(lambda: "offline-fixture", opener=opener)
