"""Public-key-only direct SSH bootstrap for the unchanged reviewed H3 image.

The host public key is published in authenticated provider logs, not learned
from the unauthenticated TCP listener. No client private key enters the Pod.
"""

from __future__ import annotations

import base64
import binascii

# This literal is the entire launch-time ENTRYPOINT override. The original
# /opt/h3-launch.py and its absolute deadline/model pins remain unchanged.
SSH_BOOTSTRAP = r'''
import json
import math
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

started = time.time()
deadline = float(os.environ["ERGO_DEADLINE"])
if not math.isfinite(deadline) or not started < deadline <= started + 3600:
    raise ValueError("existing absolute deadline must be within one hour")
cutoff = deadline - 120
if started >= cutoff:
    raise ValueError("work cutoff reached before SSH bootstrap")
child = None

def stop(signum=None, frame=None):
    if child is not None and child.poll() is None:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()
    raise SystemExit(124)

for signum in (signal.SIGALRM, signal.SIGTERM, signal.SIGINT):
    signal.signal(signum, stop)
signal.setitimer(signal.ITIMER_REAL, cutoff - time.time())

def run(argv, capture=False):
    global child
    child = subprocess.Popen(argv, start_new_session=True,
        stdout=subprocess.PIPE if capture else None)
    output, _ = child.communicate()
    if child.returncode != 0:
        raise RuntimeError("SSH bootstrap command failed; no restart")
    child = None
    return output

# The image already contains Python, apt and CA certificates, but no sshd.
# Packages are authenticated by Ubuntu's apt metadata, not an unreviewed script.
os.environ["DEBIAN_FRONTEND"] = "noninteractive"
policy = Path("/usr/sbin/policy-rc.d")
previous_policy = policy.read_bytes() if policy.exists() else None
previous_mode = policy.stat().st_mode & 0o777 if policy.exists() else None
policy.write_text("#!/bin/sh\nexit 101\n")
policy.chmod(0o755)
run(["apt-get", "-o", "Acquire::Retries=0", "-o", "Acquire::http::Timeout=15", "update"])
run(["apt-get", "-o", "Acquire::Retries=0", "-o", "Acquire::http::Timeout=15", "install", "-y", "--no-install-recommends", "openssh-server"])
if previous_policy is None:
    policy.unlink()
else:
    policy.write_bytes(previous_policy)
    policy.chmod(previous_mode)
# Unlock a root account without setting any usable password; password and
# keyboard-interactive authentication are independently disabled below.
run(["usermod", "--password", "x", "root"])
root = Path("/root/.ssh")
root.mkdir(mode=0o700, exist_ok=True)
root.chmod(0o700)
authorized = root / "ergo_authorized_keys"
authorized.write_text(os.environ.pop("ERGO_SSH_PUBLIC_KEY") + "\n")
authorized.chmod(0o600)
Path("/run/sshd").mkdir(mode=0o755, exist_ok=True)
# Do not reuse host keys that might be present in a container/template overlay.
run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", "/run/sshd/ergo_host_key"])
config = Path("/run/sshd/ergo_config")
config.write_text("""Port 22
ListenAddress 0.0.0.0
HostKey /run/sshd/ergo_host_key
PidFile /run/sshd/ergo.pid
AuthorizedKeysFile /root/.ssh/ergo_authorized_keys
PermitRootLogin prohibit-password
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
AuthenticationMethods publickey
UsePAM no
AllowUsers root
AllowTcpForwarding local
PermitOpen 127.0.0.1:30010
GatewayPorts no
X11Forwarding no
AllowAgentForwarding no
PermitTunnel no
Subsystem sftp internal-sftp
""")
run(["/usr/sbin/sshd", "-t", "-f", str(config)])
run(["/usr/sbin/sshd", "-f", str(config)])
host_key = Path("/run/sshd/ergo_host_key.pub").read_text().split()
print("ERGO_SSH_HOST_KEY " + os.environ["ERGO_ATTEMPT_FINGERPRINT"] + " " + " ".join(host_key[:2]), flush=True)
Path("/workspace/h3-access-timings.json").write_text(json.dumps({
    "started": started, "deadline": deadline,
    "ssh_setup_seconds": time.time() - started,
}))
# Establish compatibility before the original launcher can download weights.
gpu = run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
    "--format=csv,noheader,nounits"], capture=True).decode().strip().splitlines()
if len(gpu) != 1:
    raise ValueError("exactly one reviewed GPU required before model download")
name, driver, vram = [part.strip() for part in gpu[0].split(",")]
memory = int(Path("/proc/meminfo").read_text().split("MemTotal:")[1].split()[0]) * 1024
cgroup = Path("/sys/fs/cgroup/memory.max")
if cgroup.exists() and cgroup.read_text().strip() != "max":
    memory = min(memory, int(cgroup.read_text()))
disk = shutil.disk_usage("/workspace")
baseline = {
    "observed": time.time(), "deadline": deadline,
    "gpu": name, "driver": driver, "vram_mib": int(vram),
    "effective_ram_bytes": memory,
    "disk_total_bytes": disk.total, "disk_free_bytes": disk.free,
    "pre_model_seconds": time.time() - started,
}
Path("/workspace/h3-resource-baseline.json").write_text(json.dumps(baseline))
print("ERGO_H3_RESOURCE_BASELINE " + json.dumps(baseline), flush=True)
if (name != "NVIDIA RTX 6000 Ada Generation" or int(driver.split(".")[0]) < 580
        or int(vram) < 45000):
    raise ValueError("reviewed Ada 48GB and CUDA 13 driver required before model download")
# exec keeps the same PID and absolute alarm; the original launcher installs its
# own handler and resets that alarm to the SAME cutoff, never a new duration.
os.execv("/opt/venv/bin/python", ["python", "/opt/h3-launch.py"])
'''


def public_key(value):
    """Accept exactly one ordinary ed25519 public key, never options or secrets."""
    message = "one ed25519 public key required"
    if not isinstance(value, str) or "\n" in value or "\r" in value:
        raise ValueError(message)
    fields = value.split()
    if len(fields) not in (2, 3) or fields[0] != "ssh-ed25519":
        raise ValueError(message)
    message = "invalid ed25519 public key"
    try:
        wire = base64.b64decode(fields[1], validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(message) from None
    if len(wire) != 51 or wire[:19] != b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20":  # noqa: PLR2004 -- RFC8709 wire format
        raise ValueError(message)
    return " ".join(fields[:2])


def create_access(key):
    """Fixed request fields; only the reviewed public client key is variable."""
    return {
        "dockerEntrypoint": ["python", "-c", SSH_BOOTSTRAP],
        "dockerStartCmd": [],
        "ports": ["22/tcp"],
        "supportPublicIp": True,
        "env": {"ERGO_SSH_PUBLIC_KEY": public_key(key)},
    }
