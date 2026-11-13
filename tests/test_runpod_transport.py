import io
import json
import socket
import sqlite3
from urllib.error import HTTPError

import pytest

from django_ergo.plugins.runpod.transport import Ownership
from django_ergo.plugins.runpod.transport import OwnershipError
from django_ergo.plugins.runpod.transport import ProviderError
from django_ergo.plugins.runpod.transport import RateLimitError
from django_ergo.plugins.runpod.transport import RunpodREST


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        msg = "network access forbidden"
        raise AssertionError(msg)

    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)


class Response(io.BytesIO):
    pass


class HTTPFake:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return Response(json.dumps(result).encode() if result is not None else b"")


def pod(**changes):
    result = {
        "id": "pod123",
        "consumerUserId": "account1",
        "imageName": "image@sha256:123",
        "containerDiskInGb": 120,
        "volumeInGb": 0,
        "networkVolume": None,
        "env": {
            "ERGO_ATTEMPT_ID": "attempt1",
            "ERGO_ATTEMPT_FINGERPRINT": "hash1",
            "PRIVATE": "secret-value",
        },
    }
    result.update(changes)
    return result


def ownership():
    return Ownership("account1", "attempt1", "hash1", "image@sha256:123", 120)


def test_owned_delete_requires_fresh_read_and_readback_is_independent():
    missing = HTTPError("https://example.invalid/secret", 404, "secret", {}, None)
    fake = HTTPFake(pod(), None, missing)
    client = RunpodREST(lambda: "test-secret", timeout=4, opener=fake)
    client.terminate_owned("pod123", ownership())
    assert client.get_pod("pod123") is None
    assert [r.method for r, _ in fake.requests] == ["GET", "DELETE", "GET"]
    assert all(timeout == 4 for _, timeout in fake.requests)
    assert all(
        r.full_url.startswith("https://rest.runpod.io/v1/") for r, _ in fake.requests
    )
    assert all(
        r.get_header("Authorization") == "Bearer test-secret" for r, _ in fake.requests
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"consumerUserId": "other"},
        {"imageName": "other"},
        {"volumeInGb": 1},
        {"networkVolume": {"id": "foreign"}},
        {"containerDiskInGb": 121},
        {"env": {"ERGO_ATTEMPT_ID": "attempt1", "ERGO_ATTEMPT_FINGERPRINT": "other"}},
        {"env": {"ERGO_ATTEMPT_ID": "other", "ERGO_ATTEMPT_FINGERPRINT": "hash1"}},
    ],
)
def test_ownership_mismatch_never_deletes(changes):
    fake = HTTPFake(pod(**changes))
    with pytest.raises(OwnershipError):
        RunpodREST(lambda: "token", opener=fake).terminate_owned("pod123", ownership())
    assert [r.method for r, _ in fake.requests] == ["GET"]


def test_response_never_returns_unrelated_environment():
    fake = HTTPFake([pod()])
    result = RunpodREST(lambda: "token", opener=fake).list_pods()
    assert result[0]["env"] == {
        "ERGO_ATTEMPT_ID": "attempt1",
        "ERGO_ATTEMPT_FINGERPRINT": "hash1",
    }
    assert "secret-value" not in repr(result)


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("test-secret"),
        HTTPError(
            "https://example.invalid/test-secret",
            500,
            "test-secret",
            {},
            io.BytesIO(b"test-secret"),
        ),
        ValueError("test-secret"),
    ],
)
def test_errors_are_sanitized(failure):
    client = RunpodREST(lambda: "test-" + "secret", opener=HTTPFake(failure))
    with pytest.raises(ProviderError) as error:
        client.get_pod("pod123")
    import traceback

    assert "test-secret" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize(("header", "wait"), [("29", 29), ("", 60), ("99999", 3600)])
def test_rate_limit_is_distinct_and_carries_bounded_retry_after(header, wait):
    limited = HTTPError(
        "https://example.invalid/test-secret",
        429,
        "test-secret",
        {"Retry-After": header},
        io.BytesIO(b"test-secret"),
    )
    client = RunpodREST(lambda: "test-" + "secret", opener=HTTPFake(limited))
    with pytest.raises(RateLimitError) as error:
        client.get_pod("pod123")
    assert error.value.retry_after == wait
    assert "test-secret" not in repr(error.value)


def test_missing_storage_field_fails_closed():
    data = pod()
    del data["networkVolume"]
    fake = HTTPFake(data)
    with pytest.raises(OwnershipError):
        RunpodREST(lambda: "token", opener=fake).terminate_owned("pod123", ownership())
    assert len(fake.requests) == 1


class BrokenBudget:
    """A corrupt shared budget file."""

    def acquire(self, *, priority=False):
        raise sqlite3.DatabaseError

    def observe(self, headers, status):
        raise sqlite3.DatabaseError


def test_broken_budget_store_never_blocks_teardown_reads():
    fake = HTTPFake(pod())
    client = RunpodREST(lambda: "token", opener=fake, budget=BrokenBudget())
    with pytest.raises(RateLimitError):
        client.get_pod("pod123")
    assert fake.requests == []
    assert client.get_pod("pod123", priority=True)["id"] == "pod123"
