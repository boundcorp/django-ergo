"""Offline Worker tunnel lifecycle: no provider network or SSH subprocesses."""

import base64
import json
import os
import subprocess
import time
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from django_ergo.plugins.runpod import native_h3
from django_ergo.plugins.runpod import native_tunnel
from django_ergo.plugins.runpod.transport import Ownership
from django_ergo.plugins.runpod.transport import OwnershipError
from django_ergo.plugins.runpod.transport import ProviderError

KEY = (
    "ssh-ed25519 "
    + base64.b64encode(
        b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"k" * 32
    ).decode()
)
OWNER = Ownership("account", "attempt", "f" * 64, "image@sha256:" + "1" * 64, 200)


class SSH:
    def __init__(self):
        self.commands = []
        self.active = set()
        self.starts = []
        self.marker = {"fingerprint": OWNER.fingerprint, "state": "starting"}
        self.start_error = None
        self.probe_error = False

    def __call__(self, argv, **kwargs):
        assert kwargs["env"] == {"PATH": os.defpath, "LANG": "C.UTF-8"}
        assert 0 < kwargs["timeout"] <= native_tunnel.COMMAND_SECONDS
        self.commands.append((argv, kwargs))
        control = argv[argv.index("-S") + 1]
        if "-O" in argv:
            operation = argv[argv.index("-O") + 1]
            if operation == "exit":
                self.active.discard(control)
                code = 0
            else:
                code = int(control not in self.active)
            return SimpleNamespace(returncode=code, stdout=b"")
        if "-f" in argv:
            # A background master must not retain communicate() capture pipes.
            assert kwargs["stdout"] == subprocess.DEVNULL
            assert kwargs["stderr"] == subprocess.DEVNULL
            if self.start_error:
                raise self.start_error
            self.active.add(control)
            self.starts.append(argv)
            return SimpleNamespace(returncode=0, stdout=b"")
        assert "ProxyCommand=false" in argv
        assert "h3-launch-state.json" in argv[-1]
        return SimpleNamespace(
            returncode=int(self.probe_error),
            stdout=json.dumps(self.marker).encode(),
        )


@pytest.fixture
def tunnel(tmp_path, monkeypatch):
    monkeypatch.setattr(native_tunnel.tempfile, "gettempdir", lambda: str(tmp_path))
    stop = {"stop_reason": None}
    monkeypatch.setattr(
        native_tunnel, "Experiment", lambda path: SimpleNamespace(status=lambda: stop)
    )
    cleanup = SimpleNamespace(
        experiment=SimpleNamespace(_tx=lambda: nullcontext(None)),
        _profile=lambda db: OWNER,
    )
    monkeypatch.setattr(native_tunnel, "RESTCleanup", lambda path: cleanup)
    monkeypatch.setattr(
        native_tunnel, "validate_private_key", lambda policy, config: "/host/key"
    )
    ssh = SSH()
    monkeypatch.setattr(native_tunnel.subprocess, "run", ssh)
    connection = {
        "host": "203.0.113.7",
        "port": 12345,
        "host_key_alias": "ergo-" + OWNER.fingerprint,
        "known_hosts": "ergo-" + OWNER.fingerprint + " " + KEY + "\n",
    }
    provider = SimpleNamespace(ssh_connection=Mock(return_value=connection))
    job = SimpleNamespace(
        ledger_path=str(tmp_path / "rental.sqlite3"),
        profile={"binding": {"ssh_private_key_file": "/host/key"}, "config": {}},
    )
    return SimpleNamespace(
        job=job,
        provider=provider,
        pod={"id": "pod123"},
        ssh=ssh,
        deadline=time.time() + 300,
        stop=stop,
    )


def ensure(tunnel):
    return native_tunnel.ensure(
        tunnel.job, tunnel.provider, tunnel.pod, deadline=tunnel.deadline
    )


def test_failed_ssh_lookup_waits_before_spending_more_provider_reads(
    tunnel, monkeypatch
):
    clock = [time.time()]
    monkeypatch.setattr(native_tunnel.time, "time", lambda: clock[0])
    tunnel.provider.ssh_connection.side_effect = ProviderError("not yet")
    for _ in range(3):
        with pytest.raises(native_tunnel.TunnelUnavailable):
            ensure(tunnel)
        clock[0] += 5
    tunnel.provider.ssh_connection.assert_called_once()
    clock[0] += native_tunnel.CONNECT_SECONDS
    tunnel.provider.ssh_connection.side_effect = None
    assert ensure(tunnel).startswith("http://127.0.0.1:")
    assert tunnel.provider.ssh_connection.call_count == 2


def test_pins_owned_connection_and_reuses_master_across_fresh_polls(tunnel):
    first = ensure(tunnel)
    # A fresh Worker task only has the durable job, not any Popen or ctx state.
    tunnel.job = SimpleNamespace(**vars(tunnel.job))
    assert ensure(tunnel) == first
    assert len(tunnel.ssh.starts) == 1
    tunnel.provider.ssh_connection.assert_called_once()
    args, kwargs = tunnel.provider.ssh_connection.call_args
    assert args == ("pod123", OWNER)
    assert time.time() < kwargs["deadline"] <= tunnel.deadline
    argv = tunnel.ssh.starts[0]
    for option in (
        "BatchMode=yes",
        "IdentitiesOnly=yes",
        "IdentityAgent=none",
        "StrictHostKeyChecking=yes",
        "GlobalKnownHostsFile=/dev/null",
        "ControlMaster=yes",
        "ControlPersist=no",
        "ExitOnForwardFailure=yes",
        "ServerAliveInterval=5",
        "ServerAliveCountMax=2",
    ):
        assert option in argv
    assert argv[argv.index("-F") + 1] == "/dev/null"
    forwarding = argv[argv.index("-L") + 1]
    assert forwarding == first.removeprefix("http://") + ":127.0.0.1:30010"
    assert str(tunnel.deadline) in argv[-1]
    directory = native_tunnel._directory(tunnel.job)
    assert (
        directory / "known_hosts"
    ).read_text() == tunnel.provider.ssh_connection.return_value["known_hosts"]
    assert (directory / "known_hosts").stat().st_mode & 0o777 == 0o600
    assert (directory / "state.json").stat().st_mode & 0o777 == 0o600


def test_dropped_master_reauthenticates_without_recreating_rental(tunnel):
    ensure(tunnel)
    tunnel.ssh.active.clear()
    second = ensure(tunnel)
    assert second.startswith("http://127.0.0.1:")
    assert len(tunnel.ssh.starts) == 2
    assert tunnel.provider.ssh_connection.call_count == 2
    assert all(str(tunnel.deadline) in argv[-1] for argv in tunnel.ssh.starts)


def test_jobs_have_distinct_control_sockets_and_dynamic_forward_ports(tunnel):
    first = ensure(tunnel)
    first_socket = tunnel.ssh.starts[-1][tunnel.ssh.starts[-1].index("-S") + 1]
    tunnel.job = SimpleNamespace(
        ledger_path=tunnel.job.ledger_path + "-second", profile=tunnel.job.profile
    )
    second = ensure(tunnel)
    second_socket = tunnel.ssh.starts[-1][tunnel.ssh.starts[-1].index("-S") + 1]
    assert first_socket != second_socket
    assert first.startswith("http://127.0.0.1:")
    assert second.startswith("http://127.0.0.1:")


def test_close_from_fresh_controller_is_idempotent(tunnel):
    ensure(tunnel)
    fresh_job = SimpleNamespace(ledger_path=tunnel.job.ledger_path)
    native_tunnel.close(fresh_job)
    native_tunnel.close(fresh_job)
    assert not tunnel.ssh.active
    assert not (native_tunnel._directory(tunnel.job) / "state.json").exists()


def test_launcher_failure_is_observed_before_health_or_startup_deadline(tunnel):
    tunnel.ssh.marker["state"] = "failed"
    with pytest.raises(native_tunnel.LauncherFailed, match="no model restart"):
        ensure(tunnel)
    assert not tunnel.ssh.active
    assert not (native_tunnel._directory(tunnel.job) / "state.json").exists()


def test_foreign_launcher_marker_fails_closed(tunnel):
    tunnel.ssh.marker["fingerprint"] = "wrong-attempt"
    with pytest.raises(native_tunnel.TunnelUnavailable):
        ensure(tunnel)
    assert not tunnel.ssh.active


def test_failed_probe_closes_existing_master(tunnel):
    ensure(tunnel)
    tunnel.ssh.probe_error = True
    with pytest.raises(native_tunnel.TunnelUnavailable):
        ensure(tunnel)
    assert not tunnel.ssh.active


def test_failed_reauthentication_after_drop_closes_local_state(tunnel):
    ensure(tunnel)
    tunnel.ssh.active.clear()
    tunnel.provider.ssh_connection.side_effect = ProviderError("fixture")
    with pytest.raises(native_tunnel.TunnelUnavailable):
        ensure(tunnel)
    assert len(tunnel.ssh.starts) == 1
    assert not tunnel.ssh.active
    assert not (native_tunnel._directory(tunnel.job) / "state.json").exists()


@pytest.mark.parametrize("error", [ProviderError("fixture"), OwnershipError("fixture")])
def test_unverified_connection_never_starts_ssh(tunnel, error):
    tunnel.provider.ssh_connection.side_effect = error
    with pytest.raises((native_tunnel.TunnelUnavailable, OwnershipError)):
        ensure(tunnel)
    assert not tunnel.ssh.starts


def test_bounded_start_failure_cleans_control_state(tunnel):
    tunnel.ssh.start_error = subprocess.TimeoutExpired("ssh", 5)
    with pytest.raises(native_tunnel.TunnelUnavailable):
        ensure(tunnel)
    assert not tunnel.ssh.active
    assert not (native_tunnel._directory(tunnel.job) / "state.json").exists()


def test_stop_during_open_closes_new_tunnel(tunnel):
    original = tunnel.provider.ssh_connection.side_effect

    def cancel(*args, **kwargs):
        tunnel.stop["stop_reason"] = "cancelled"
        return tunnel.provider.ssh_connection.return_value

    assert original is None
    tunnel.provider.ssh_connection.side_effect = cancel
    with pytest.raises(native_tunnel.TunnelUnavailable):
        ensure(tunnel)
    assert not tunnel.ssh.active
    assert not tunnel.ssh.starts


def test_stop_observed_after_start_closes_new_master(tunnel, monkeypatch):
    probe = native_tunnel._launcher

    def cancel(*args):
        probe(*args)
        tunnel.stop["stop_reason"] = "cancelled"

    monkeypatch.setattr(native_tunnel, "_launcher", cancel)
    with pytest.raises(native_tunnel.TunnelUnavailable):
        ensure(tunnel)
    assert len(tunnel.ssh.starts) == 1
    assert not tunnel.ssh.active


def test_durable_stop_never_opens_new_connection(tunnel):
    tunnel.stop["stop_reason"] = "cancelled"
    with pytest.raises(native_tunnel.TunnelUnavailable):
        ensure(tunnel)
    tunnel.provider.ssh_connection.assert_not_called()
    assert not tunnel.ssh.starts


def test_expired_cutoff_never_opens_ssh(tunnel):
    tunnel.deadline = time.time() - 1
    # No authentication or new SSH process is allowed beyond the original cutoff.
    with pytest.raises(native_tunnel.TunnelUnavailable):
        ensure(tunnel)
    assert not tunnel.ssh.starts
    tunnel.provider.ssh_connection.assert_not_called()


def test_host_private_key_is_validated_without_serializing_or_exposing_it(
    tmp_path, monkeypatch
):
    key = tmp_path / "key"
    key.write_text("offline opaque key fixture")
    key.chmod(0o600)
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=KEY.encode()))
    monkeypatch.setattr(native_tunnel.subprocess, "run", run)
    assert native_tunnel.validate_private_key(
        {"ssh_private_key_file": str(key)}, {"ssh_public_key": KEY}
    ) == str(key)
    assert run.call_args.args[0] == ["ssh-keygen", "-y", "-P", "", "-f", str(key)]
    assert run.call_args.kwargs["env"] == {"PATH": os.defpath, "LANG": "C.UTF-8"}
    key.chmod(0o644)
    with pytest.raises(ValueError, match="Host SSH private key"):
        native_tunnel.validate_private_key(
            {"ssh_private_key_file": str(key)}, {"ssh_public_key": KEY}
        )
    assert run.call_count == 1


@pytest.mark.parametrize(
    ("code", "stdout"),
    [(1, b""), (0, (KEY[:-4] + "cXFx").encode())],
)
def test_private_key_mismatch_and_secret_diagnostics_are_rejected(
    tmp_path, monkeypatch, code, stdout
):
    key = tmp_path / "key"
    key.write_text("offline opaque key fixture")
    key.chmod(0o600)
    monkeypatch.setattr(
        native_tunnel.subprocess,
        "run",
        Mock(
            return_value=SimpleNamespace(
                returncode=code, stdout=stdout, stderr=b"secret"
            )
        ),
    )
    with pytest.raises(
        ValueError, match="^Host SSH private key must match the approved public key$"
    ):
        native_tunnel.validate_private_key(
            {"ssh_private_key_file": str(key)}, {"ssh_public_key": KEY}
        )


def test_worker_step_surfaces_launcher_failure_without_waiting(tunnel, monkeypatch):
    tunnel.job.created = time.time()
    tunnel.job.profile["config"]["timeline"] = {
        "startup_seconds": 600,
        "download_seconds": 600,
    }
    state = {
        "deadline": tunnel.deadline + 120,
        "phase": "provisioning",
        "created": tunnel.job.created,
    }
    monkeypatch.setattr(
        native_h3, "Experiment", lambda path: SimpleNamespace(status=lambda: state)
    )
    monkeypatch.setattr(native_h3.native, "invalid_owner", lambda job: False)
    monkeypatch.setattr(native_h3.native, "client", lambda job: tunnel.provider)
    monkeypatch.setattr(native_h3, "_owned_pod", lambda job, provider: tunnel.pod)
    monkeypatch.setattr(
        native_h3,
        "CleanerSupervision",
        lambda path: SimpleNamespace(require_fresh=lambda **kwargs: None),
    )
    health = Mock()
    monkeypatch.setattr(native_h3, "H3Client", health)
    tunnel.ssh.marker["state"] = "failed"
    with pytest.raises(native_tunnel.LauncherFailed):
        native_h3.step(SimpleNamespace(stopping=False, state={}), tunnel.job)
    health.assert_not_called()
    assert not tunnel.ssh.active
