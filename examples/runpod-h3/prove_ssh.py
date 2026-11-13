"""CPU-only proof of the exact SSH override on the preserved image; no provider.

Runs two disposable local containers with no public published ports. Only the
client container receives a disposable proof private key. GPU/HF/SGLang commands
are explicit fixtures, so this proves transport, not model execution or fit.
Ubuntu package downloads are required; checkpoints are never requested.
"""

# Fixed local Docker/SSH executables; deliberately an explicit operator smoke.
# ruff: noqa: S603, S607
from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

from django_ergo.plugins.runpod.ssh import SSH_BOOTSTRAP
from django_ergo.plugins.runpod.ssh import public_key

IMAGE = "registry.octo.boundcorp.net/ergo-h3-experiment/h3-runtime@sha256:7b08f5d6b56988afeab528c9524ded9052a6739df0899768294134afe1453f8b"
CONFIG = "sha256:74ae80e663556a495683e8fdae6cec1981e4d48b5e237902dc4880f28b2c2f5c"


def run(*args, payload=None, timeout=120):
    result = subprocess.run(
        args, input=payload, capture_output=True, timeout=timeout, check=False
    )
    if result.returncode:
        message = result.stderr.decode(errors="replace")[-2000:]
        raise RuntimeError(message)
    return result.stdout


def fixture_tar():
    files = {
        "hf": "#!/bin/sh\necho 'CPU proof fixture: no checkpoint download'\nexit 0\n",
        "nvidia-smi": "#!/bin/sh\necho 'NVIDIA RTX 6000 Ada Generation, 580.65.06, 49140'\n",
        "sglang": """#!/opt/venv/bin/python
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        content = b'{"status":"ok"}' if self.path == '/health' else b'cpu-proof-artifact'
        self.send_response(200)
        self.end_headers()
        self.wfile.write(content)
HTTPServer(('127.0.0.1', 30010), Handler).serve_forever()
""",
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        directory = tarfile.TarInfo("ergo-ssh-proof")
        directory.type, directory.mode = tarfile.DIRTYPE, 0o755
        archive.addfile(directory)
        for name, content in files.items():
            payload = content.encode()
            info = tarfile.TarInfo("ergo-ssh-proof/" + name)
            info.mode, info.size = 0o755, len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def main():  # noqa: C901, PLR0912, PLR0915 -- ordered disposable transport proof
    start = time.time()
    fingerprint = uuid.uuid4().hex * 2
    server, client = (
        "h3-ssh-proof-" + uuid.uuid4().hex,
        "h3-client-proof-" + uuid.uuid4().hex,
    )
    created = []
    with tempfile.TemporaryDirectory(prefix="h3-ssh-proof-") as directory:
        root = Path(directory)
        try:
            image = json.loads(run("docker", "image", "inspect", IMAGE))[0]
            if image["Id"] != CONFIG:
                message = "preserved image config digest mismatch"
                raise ValueError(message)
            original_path = next(
                value[5:]
                for value in image["Config"]["Env"]
                if value.startswith("PATH=")
            )
            key = root / "proof-key"
            wrong = root / "wrong-key"
            for path in (key, wrong):
                run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path))
            run(
                "docker",
                "create",
                "--name",
                server,
                "--cpus",
                "2",
                "--memory",
                "6g",
                "--env",
                "ERGO_DEADLINE=" + str(time.time() + 900),
                "--env",
                "ERGO_ATTEMPT_FINGERPRINT=" + fingerprint,
                "--env",
                "ERGO_SSH_PUBLIC_KEY="
                + public_key(key.with_suffix(".pub").read_text().strip()),
                "--env",
                "PATH=/tmp/ergo-ssh-proof:" + original_path,
                "--entrypoint",
                "python",
                IMAGE,
                "-c",
                SSH_BOOTSTRAP,
            )
            created.append(server)
            run("docker", "cp", "-", server + ":/tmp", payload=fixture_tar())
            run("docker", "start", server)
            limit = time.time() + 300
            host_key = None
            while time.time() < limit:
                logs = run("docker", "logs", server).decode(errors="replace")
                for line in logs.splitlines():
                    prefix = "ERGO_SSH_HOST_KEY " + fingerprint + " "
                    if line.startswith(prefix):
                        host_key = public_key(line[len(prefix) :])
                if host_key:
                    break
                if (
                    run(
                        "docker", "inspect", "--format", "{{.State.Running}}", server
                    ).strip()
                    != b"true"
                ):
                    message = (
                        "bootstrap exited before authenticated host-key publication"
                    )
                    raise RuntimeError(message)
                time.sleep(1)
            if not host_key:
                message = "CPU SSH bootstrap exceeded five-minute proof allowance"
                raise TimeoutError(message)
            run(
                "docker",
                "run",
                "-d",
                "--name",
                client,
                "--cpus",
                "1",
                "--memory",
                "1g",
                "--network",
                "container:" + server,
                "--entrypoint",
                "sleep",
                IMAGE,
                "600",
            )
            created.append(client)
            run(
                "docker",
                "exec",
                "--env",
                "DEBIAN_FRONTEND=noninteractive",
                client,
                "sh",
                "-c",
                "apt-get update && apt-get install -y --no-install-recommends openssh-client",
                timeout=240,
            )
            known = root / "known_hosts"
            known.write_text("cpu-proof " + host_key + "\n")
            for path in (key, wrong, known):
                run("docker", "cp", str(path), client + ":/root/" + path.name)
            ssh = [
                "ssh",
                "-F",
                "/dev/null",
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "UserKnownHostsFile=/root/known_hosts",
                "-o",
                "HostKeyAlias=cpu-proof",
                "-o",
                "ConnectTimeout=5",
                "-i",
                "/root/proof-key",
            ]
            run("docker", "exec", client, *ssh, "root@127.0.0.1", "true")
            unauthorized = subprocess.run(
                [
                    "docker",
                    "exec",
                    client,
                    *ssh[:-1],
                    "/root/wrong-key",
                    "root@127.0.0.1",
                    "true",
                ],
                capture_output=True,
                timeout=15,
                check=False,
            )
            if unauthorized.returncode == 0:
                message = "unapproved client key was accepted"
                raise AssertionError(message)
            bad_known = root / "bad_known_hosts"
            bad_known.write_text("cpu-proof " + wrong.with_suffix(".pub").read_text())
            run("docker", "cp", str(bad_known), client + ":/root/known_hosts")
            rejected = subprocess.run(
                ["docker", "exec", client, *ssh, "root@127.0.0.1", "true"],
                capture_output=True,
                timeout=15,
                check=False,
            )
            if (
                rejected.returncode == 0
                or b"Host key verification failed" not in rejected.stderr
            ):
                message = "strict host-key mismatch was not rejected"
                raise AssertionError(message)
            run("docker", "cp", str(known), client + ":/root/known_hosts")
            run(
                "docker",
                "exec",
                client,
                *ssh,
                "-o",
                "ExitOnForwardFailure=yes",
                "-f",
                "-N",
                "-L",
                "127.0.0.1:30011:127.0.0.1:30010",
                "root@127.0.0.1",
            )
            proof = "from urllib.request import urlopen; import json; assert json.load(urlopen('http://127.0.0.1:30011/health'))['status']=='ok'; assert urlopen('http://127.0.0.1:30011/fixture.bin').read()==b'cpu-proof-artifact'"
            for _ in range(20):
                attempt = subprocess.run(
                    ["docker", "exec", client, "python", "-c", proof],
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
                if attempt.returncode == 0:
                    break
                time.sleep(1)
            else:
                message = "authenticated loopback tunnel did not deliver fixture health/artifact"
                raise AssertionError(message)
            scp = [
                "scp",
                "-F",
                "/dev/null",
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "UserKnownHostsFile=/root/known_hosts",
                "-o",
                "HostKeyAlias=cpu-proof",
                "-i",
                "/root/proof-key",
            ]
            for name in (
                "h3-access-timings.json",
                "h3-timings.json",
                "h3-resource-baseline.json",
            ):
                run(
                    "docker",
                    "exec",
                    client,
                    *scp,
                    "root@127.0.0.1:/workspace/" + name,
                    "/root/" + name,
                )
                run("docker", "cp", client + ":/root/" + name, str(root / name))
            print(
                json.dumps(
                    {
                        "proof": "CPU fixtures only; no provider/GPU/checkpoint calls",
                        "image_config": image["Id"],
                        "strict_host_pin": True,
                        "wrong_client_key_rejected": True,
                        "wrong_host_key_rejected": True,
                        "loopback_http_and_binary": True,
                        "scp_timings_and_baseline": True,
                        "ssh_setup": json.loads(
                            (root / "h3-access-timings.json").read_text()
                        ),
                        "synthetic_gpu_baseline": json.loads(
                            (root / "h3-resource-baseline.json").read_text()
                        ),
                        "proof_seconds": time.time() - start,
                    },
                    indent=2,
                )
            )
        finally:
            # Fixture-only container environment contains no provider/private
            # client credentials; preserve the actual bootstrap failure evidence.
            if sys.exc_info()[0] is not None and server in created:
                diagnostic = subprocess.run(
                    ["docker", "logs", "--tail", "80", server],
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
                print(
                    json.dumps(
                        {
                            "bootstrap_failure_log": (
                                diagnostic.stdout + diagnostic.stderr
                            ).decode(errors="replace")[-12000:]
                        }
                    ),
                    flush=True,
                )
            for name in reversed(created):
                subprocess.run(
                    ["docker", "rm", "-f", name],
                    capture_output=True,
                    timeout=30,
                    check=False,
                )


if __name__ == "__main__":
    main()
