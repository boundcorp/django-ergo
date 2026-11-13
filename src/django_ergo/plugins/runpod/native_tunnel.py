"""Worker-owned, deadline-bounded SSH forwarding to the H3 loopback listener.

OpenSSH's per-job control socket survives fresh Worker polling tasks. No PID is
trusted or killed, no fixed host port is shared, and every new connection pins
its key through the authenticated provider ownership/log handshake.
"""

# Fixed OpenSSH executables and authenticated numeric addresses, never a shell.
# ruff: noqa: S603, S607, SLF001
from __future__ import annotations

import hashlib
import json
import os
import shlex
import socket
import stat
import subprocess
import tempfile
import time
from contextlib import suppress
from pathlib import Path

from .cleanup import RESTCleanup
from .experiment import Experiment
from .ssh import public_key
from .transport import OwnershipError
from .transport import ProviderError

START_SECONDS = 15
COMMAND_SECONDS = 5
# Each authenticated SSH lookup costs up to three RunPod REST reads (Pod, logs,
# Pod) from a small per-key budget; retry it at most once a minute.
CONNECT_SECONDS = 60


class TunnelUnavailable(OSError):  # noqa: N818 -- availability signal for pre-ready polling
    """The owned rental has no usable authenticated tunnel yet."""


class LauncherFailed(RuntimeError):  # noqa: N818 -- terminal launcher lifecycle signal
    """The authenticated rental reported a terminal launcher failure."""

    def __init__(self, message, *, marker=None):
        super().__init__(message)
        self.marker = marker


def validate_private_key(policy, config):
    """Validate a host-only identity path against the rental's public key.

    Only the path is returned/snapshotted. Neither key contents nor subprocess
    diagnostics enter provider requests, Worker state, logs or exceptions.
    """
    message = "Host SSH private key must match the approved public key"
    try:
        value = policy["ssh_private_key_file"]
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError(message)  # noqa: TRY301 -- sanitize all key failures below
        path = Path(value).resolve(strict=True)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError(message)  # noqa: TRY301 -- sanitize all key failures below
        result = subprocess.run(
            ["ssh-keygen", "-y", "-P", "", "-f", str(path)],
            capture_output=True,
            timeout=COMMAND_SECONDS,
            check=False,
            env={"PATH": os.defpath, "LANG": "C.UTF-8"},
        )
        if result.returncode or public_key(
            result.stdout.decode().strip()
        ) != public_key(config["ssh_public_key"]):
            raise ValueError(message)  # noqa: TRY301 -- sanitize all key failures below
    except (KeyError, OSError, ValueError, subprocess.SubprocessError):
        raise ValueError(message) from None
    return str(path)


def _directory(job):
    identity = hashlib.sha256(str(Path(job.ledger_path).resolve()).encode()).hexdigest()
    directory = Path(tempfile.gettempdir()) / ("ergo-h3-" + identity[:32])
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        message = "Private Worker tunnel directory required"
        raise TunnelUnavailable(message)
    return directory


def _run(argv, deadline, *, capture=False):
    remaining = min(COMMAND_SECONDS, deadline - time.time())
    if remaining <= 0:
        raise TimeoutError
    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=remaining,
        check=False,
        env={"PATH": os.defpath, "LANG": "C.UTF-8"},
    )


def _control(directory, command, deadline):
    return _run(
        [
            "ssh",
            "-F",
            "/dev/null",
            "-S",
            str(directory / "master"),
            "-O",
            command,
            "root@127.0.0.1",
        ],
        deadline,
    )


def _close(directory):
    # A missing/dead master is already closed. Its remote watchdog still bounds
    # any connection whose local control command cannot complete.
    with suppress(OSError, subprocess.SubprocessError):
        _control(directory, "exit", time.time() + 2)
    with suppress(OSError):
        (directory / "state.json").unlink(missing_ok=True)


def close(job):
    """Close this host's job tunnel, independently of provider cleanup."""
    # Local transport state must never obstruct durable stop or independent
    # provider cleanup, even after filesystem loss or a controller restart.
    with suppress(OSError):
        _close(_directory(job))


def _write(path, value):
    temporary = path.with_suffix(".next")
    temporary.write_text(value)
    temporary.chmod(0o600)
    temporary.replace(path)


def _options(directory, connection, key):
    return [
        "ssh",
        "-F",
        "/dev/null",
        "-S",
        str(directory / "master"),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "UserKnownHostsFile=" + str(directory / "known_hosts"),
        "-o",
        "HostKeyAlias=" + connection["host_key_alias"],
        "-o",
        "ConnectTimeout=5",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        "ExitOnForwardFailure=yes",
        "-i",
        key,
        "-p",
        str(connection["port"]),
    ]


def _launcher(directory, state, fingerprint, deadline):
    # ProxyCommand=false forbids OpenSSH silently opening an independent
    # connection if the checked master disappears before this command.
    script = (
        "from pathlib import Path; "
        "p=Path('/workspace/h3-launch-state.json'); "
        "print(p.read_text() if p.is_file() else '{}')"
    )
    result = _run(
        [
            *_options(directory, state["connection"], state["key"]),
            "-o",
            "ControlMaster=no",
            "-o",
            "ProxyCommand=false",
            "root@" + state["connection"]["host"],
            "/opt/venv/bin/python -c " + shlex.quote(script),
        ],
        deadline,
        capture=True,
    )
    if result.returncode:
        message = "Authenticated launcher probe unavailable"
        raise TunnelUnavailable(message)
    marker = json.loads(result.stdout)
    if not isinstance(marker, dict):
        message = "Authenticated launcher marker invalid"
        raise TunnelUnavailable(message)
    if marker and (
        marker.get("fingerprint") != fingerprint
        or marker.get("state") not in {"starting", "ready", "failed"}
    ):
        message = "Authenticated launcher marker invalid"
        raise TunnelUnavailable(message)
    if marker.get("state") == "failed":
        message = "H3 launcher failed; no model restart or generation replay"
        raise LauncherFailed(message, marker=marker)


def ensure(job, provider, pod, *, deadline):  # noqa: C901, PLR0915 -- one bounded tunnel lifecycle
    """Maintain/recover one job's tunnel and return its private loopback URL.

    Called under the native job operation lock. The control socket and metadata
    retain transport state across tasks, not in process memory or ctx.state.
    A reconnect is transport-only: it never restarts the launcher or generation.
    """
    directory = _directory(job)
    if time.time() >= deadline:
        _close(directory)
        message = "Rental work cutoff reached; no SSH connection"
        raise TunnelUnavailable(message)
    end = min(deadline, time.time() + START_SECONDS)
    cleanup = RESTCleanup(job.ledger_path)
    with cleanup.experiment._tx() as db:
        owner = cleanup._profile(db)
    try:
        state_path = directory / "state.json"
        state = json.loads(state_path.read_text()) if state_path.is_file() else None
        active = bool(
            state
            and state["pod_id"] == pod["id"]
            and state["fingerprint"] == owner.fingerprint
            and state["deadline"] == deadline
            and _control(directory, "check", end).returncode == 0
        )
        if not active:
            _close(directory)
            if _control(directory, "check", end).returncode == 0:
                message = "Previous SSH tunnel did not close"
                raise TunnelUnavailable(message)
            if Experiment(job.ledger_path).status()["stop_reason"]:
                message = "Rental stopped; no new SSH connection"
                raise TunnelUnavailable(message)
            key = validate_private_key(job.profile["binding"], job.profile["config"])
            after = directory / "connect-after"
            try:
                waiting = time.time() < float(after.read_text())
            except (OSError, ValueError):
                waiting = False  # absent or unreadable: allow one lookup now
            if waiting:
                message = "Waiting to retry the authenticated SSH lookup"
                raise TunnelUnavailable(message)
            _write(after, repr(time.time() + CONNECT_SECONDS))
            connection = provider.ssh_connection(pod["id"], owner, deadline=end)
            if Experiment(job.ledger_path).status()["stop_reason"]:
                message = "Rental stopped during SSH authentication"
                raise TunnelUnavailable(message)
            _write(directory / "known_hosts", connection["known_hosts"])
            # Let the kernel choose a distinct loopback port. The short release
            # race fails closed through ExitOnForwardFailure, never through a
            # request to somebody else's pre-existing local listener.
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            watchdog = (
                "import time; time.sleep(max(0," + repr(deadline) + "-time.time()))"
            )
            argv = [
                *_options(directory, connection, key),
                "-o",
                "ControlMaster=yes",
                "-o",
                "ControlPersist=no",
                "-n",
                "-T",
                "-f",
                "-L",
                f"127.0.0.1:{port}:127.0.0.1:30010",
                "root@" + connection["host"],
                "/opt/venv/bin/python -c " + shlex.quote(watchdog),
            ]
            started = _run(argv, end)
            if started.returncode or _control(directory, "check", end).returncode:
                message = "Authenticated SSH tunnel did not start"
                raise TunnelUnavailable(message)
            state = {
                "pod_id": pod["id"],
                "fingerprint": owner.fingerprint,
                "deadline": deadline,
                "connection": connection,
                "key": key,
                "port": port,
            }
            _write(state_path, json.dumps(state))
            # A later transport loss may reconnect at once; only lookups that
            # did not yield a tunnel wait out CONNECT_SECONDS.
            after.unlink(missing_ok=True)
        _launcher(directory, state, owner.fingerprint, end)
        if (
            Experiment(job.ledger_path).status()["stop_reason"]
            or time.time() >= deadline
        ):
            message = "Rental stopped; SSH tunnel closed"
            raise TunnelUnavailable(message)
        return f"http://127.0.0.1:{state['port']}"
    except LauncherFailed:
        _close(directory)
        raise
    except OwnershipError:
        _close(directory)
        raise
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        ProviderError,
        subprocess.SubprocessError,
    ):
        _close(directory)
        message = "Owned rental SSH tunnel unavailable"
        raise TunnelUnavailable(message) from None
