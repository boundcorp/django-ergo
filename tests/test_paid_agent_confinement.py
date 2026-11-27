"""External mock harness: real local namespaces; no production runtime claim."""

import json
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from django_ergo.bots.agents import AgentSpec
from django_ergo.plugins.local_shell import LocalShellError
from django_ergo.plugins.paid_codex import PaidCodexPlugin


@pytest.mark.parametrize("claimed", ["tests.test_paid_codex.MockSandbox", True, "env"])
def test_claimed_backend_cannot_open_production_gate(tmp_path, settings, claimed):
    settings.PAID_AGENT_SANDBOX_BACKEND = claimed
    settings.PAID_AGENT_CONFINEMENT_VERIFIED = True
    plugin = PaidCodexPlugin(
        None, {"root": str(tmp_path), "proxy_url": "https://proxy.invalid/v1"}
    )
    plugin.on_load()
    with pytest.raises(LocalShellError, match="no verified"):
        plugin.start_agent(
            None,
            AgentSpec(
                agent="codex",
                workspace="job",
                brief="hi",
                confinement={"agent": "codex"},
            ),
        )


PROBE = r"""
import glob, json, os, socket, subprocess, sys

def probe():
    visible = False
    for path in glob.glob('/proc/[0-9]*/environ'):
        try:
            visible |= b'OPENAI_API_KEY=' in open(path, 'rb').read()
        except OSError:
            pass
    denied = []
    # All targets are mock/synthetic; the isolated net namespace has no route.
    for target in [('127.0.0.1', int(sys.argv[1])), ('1.1.1.1', 443)]:
        sock = socket.socket()
        sock.settimeout(0.3)
        denied.append(sock.connect_ex(target) != 0)
        sock.close()
    client = socket.socket(socket.AF_UNIX)
    client.connect('/proxy/mock.sock')
    client.sendall(b'GET /health HTTP/1.0\r\n\r\n')
    reached = b'200 OK' in client.recv(1024)
    client.close()
    return {'key_denied': not visible and 'OPENAI_API_KEY' not in os.environ,
            'host_file_denied': not os.path.exists(sys.argv[2]),
            'direct_egress_denied': all(denied), 'proxy_reachable': reached}
result = probe()
if len(sys.argv) == 3:
    result['descendant'] = json.loads(subprocess.check_output([sys.executable, __file__, *sys.argv[1:], 'child']))
print(json.dumps(result))
"""


def test_external_namespace_harness_denies_key_and_direct_egress(tmp_path):
    if not shutil.which("bwrap"):
        pytest.skip("Local mock confinement harness requires bubblewrap")
    sockets = tmp_path / "sockets"
    sockets.mkdir()
    secret = tmp_path / "host-only-key"
    secret.write_text("FAKE sentinel only")
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE)
    requests = []

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            requests.append(self.request.recv(1024))
            self.request.sendall(b"HTTP/1.0 200 OK\r\n\r\nmock proxy")

    server = socketserver.UnixStreamServer(str(sockets / "mock.sock"), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    upstream = socket.socket()
    upstream.bind(("127.0.0.1", 0))
    upstream.listen()
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env={"OPENAI_API_KEY": "FAKE external sentinel"},
    )
    argv = [
        "bwrap",
        "--unshare-all",
        "--new-session",
        "--die-with-parent",
        "--cap-drop",
        "ALL",
    ]
    for path in ("/usr", "/lib", "/lib64"):
        if Path(path).exists():
            argv.extend(["--ro-bind", path, path])
    argv.extend(
        [
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--ro-bind",
            str(sockets),
            "/proxy",
            "--ro-bind",
            str(probe),
            "/probe.py",
            "--clearenv",
            "--setenv",
            "PATH",
            "/usr/bin",
            "/usr/bin/python3",
            "/probe.py",
            str(upstream.getsockname()[1]),
            str(secret),
        ]
    )
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, check=True, timeout=15
        )
        result = json.loads(completed.stdout)
        descendant = result.pop("descendant")
        assert all(result.values()) and all(descendant.values()), result
        assert len(requests) == 2
    finally:
        sentinel.terminate()
        sentinel.wait(timeout=5)
        upstream.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
