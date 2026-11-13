"""Offline contracts for launch-time SSH and authenticated host-key discovery."""

import base64
import io
import json
import time
from unittest.mock import Mock

import pytest

from django_ergo.plugins.runpod.ssh import SSH_BOOTSTRAP
from django_ergo.plugins.runpod.ssh import create_access
from django_ergo.plugins.runpod.ssh import public_key
from django_ergo.plugins.runpod.transport import Ownership
from django_ergo.plugins.runpod.transport import OwnershipError
from django_ergo.plugins.runpod.transport import ProviderError
from django_ergo.plugins.runpod.transport import RunpodREST

KEY = (
    "ssh-ed25519 "
    + base64.b64encode(
        b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"k" * 32
    ).decode()
)
OWNER = Ownership("account", "attempt", "f" * 64, "image@sha256:" + "1" * 64, 200)


def pod(**changes):
    value = {
        "id": "pod123",
        "consumerUserId": OWNER.account,
        "imageName": OWNER.image,
        "containerDiskInGb": 200,
        "volumeInGb": 0,
        "env": {
            "ERGO_ATTEMPT_ID": OWNER.attempt,
            "ERGO_ATTEMPT_FINGERPRINT": OWNER.fingerprint,
        },
        "publicIp": "203.0.113.7",
        "portMappings": {"22": 12345},
    }
    value.update(changes)
    return value


class HTTP:
    def __init__(self, *results):
        self.results = list(results)
        self.requests = []

    def open(self, request, timeout):
        assert 0 < timeout <= 30
        assert request.get_header("User-agent") == "Ergonaut-native-preflight"
        assert request.get_header("Authorization") == "Bearer fixture-secret"
        self.requests.append(request)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return io.BytesIO(
            result if isinstance(result, bytes) else json.dumps(result).encode()
        )


def logs(key=KEY, fingerprint=OWNER.fingerprint):
    event = {
        "source": "container",
        "line": "ERGO_SSH_HOST_KEY " + fingerprint + " " + key,
        "ts": "2026-11-12T00:00:00Z",
    }
    return b": keepalive\n\nid: 1\ndata: " + json.dumps(event).encode() + b"\n\n"


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        KEY + "\n",
        "command=x " + KEY,
        "ssh-rsa AAAA",
        "ssh-ed25519 AAAA",
        "-----BEGIN OPENSSH " + "PRIVATE KEY-----",
    ],
)
def test_private_keys_options_and_malformed_keys_rejected(value):
    with pytest.raises(
        ValueError, match="ed25519 public key required|invalid ed25519 public key"
    ):
        public_key(value)


def test_authenticated_logs_pin_exact_owned_address():
    http = HTTP(pod(), logs(), pod())
    connection = RunpodREST(lambda: "fixture-secret", opener=http).ssh_connection(
        "pod123", OWNER, deadline=time.time() + 30
    )
    alias = "ergo-" + OWNER.fingerprint
    assert connection == {
        "host": "203.0.113.7",
        "port": 12345,
        "host_key_alias": alias,
        "known_hosts": alias + " " + KEY + "\n",
    }
    assert [r.method for r in http.requests] == ["GET", "GET", "GET"]
    assert (
        http.requests[1].full_url
        == "https://api.runpod.io/v2/pods/pod123/logs?source=container&tail=5000"
    )


@pytest.mark.parametrize(
    "bad",
    [
        logs(fingerprint="wrong"),
        logs(key="ssh-ed25519 AAAA"),
        b"",
        RuntimeError("fixture-secret"),
    ],
)
def test_missing_untrusted_or_unavailable_log_key_fails_closed(bad):
    http = HTTP(pod(), bad)
    with pytest.raises(ProviderError, match="authenticated SSH connection unverified"):
        RunpodREST(lambda: "fixture-secret", opener=http).ssh_connection(
            "pod123", OWNER, deadline=time.time() + 30
        )
    assert len(http.requests) == 2


def test_address_change_during_pin_fails_closed():
    http = HTTP(pod(), logs(), pod(publicIp="203.0.113.8"))
    with pytest.raises(ProviderError):
        RunpodREST(lambda: "fixture-secret", opener=http).ssh_connection(
            "pod123", OWNER, deadline=time.time() + 30
        )


def test_foreign_pod_never_reads_logs():
    http = HTTP(pod(consumerUserId="foreign"))
    with pytest.raises(OwnershipError):
        RunpodREST(lambda: "fixture-secret", opener=http).ssh_connection(
            "pod123", OWNER, deadline=time.time() + 30
        )
    assert len(http.requests) == 1


RULES = {
    "gpu_count": 1,
    "nvml_names": ["NVIDIA A100-SXM4-80GB"],
    "compute_capabilities": [],
    "min_vram_mib": 45000,
    "min_driver_major": 580,
}


@pytest.mark.parametrize(
    "rules",
    [
        None,
        ["A100-SXM4-80GB"],
        {**RULES, "nvml_names": []},
        {**RULES, "nvml_names": "A100-SXM4-80GB"},
        {**RULES, "min_vram_mib": "45000"},
        {key: value for key, value in RULES.items() if key != "min_driver_major"},
    ],
)
def test_bootstrap_requires_explicit_gpu_rules(rules):
    with pytest.raises(ValueError, match="Verified NVML names or allowed compute"):
        create_access(KEY, "NVIDIA A100-SXM4-80GB", rules)


def test_expired_bootstrap_never_installs_packages(monkeypatch):
    monkeypatch.setenv("ERGO_DEADLINE", str(time.time() - 1))
    spawn = Mock()
    monkeypatch.setattr("subprocess.Popen", spawn)
    with pytest.raises(ValueError, match="absolute deadline"):
        exec(compile(SSH_BOOTSTRAP, "bootstrap", "exec"), {})
    spawn.assert_not_called()
