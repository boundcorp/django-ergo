"""``ergonaut up``: run everything in one container.

Each backing service starts only when its URL is not set, so the same
command runs fully self-contained on a laptop and as a thin app container
next to managed Postgres, Redis and S3:

=====================  ============================================  ==============
Setting                Unset                                         Set
=====================  ============================================  ==============
``DATABASE_URL``       embedded Postgres + pgvector (pgserver)       use it
``CELERY_BROKER_URL``  ``redis-server`` if installed, else no queue  use it
``S3_ENDPOINT_URL``    ``garage`` if installed, else local files     use it
=====================  ============================================  ==============

Then it runs the web app, the Celery worker and beat (when there is a
broker) and the bot runner, and stops them all if any one exits. Data
lives under ``DATA_DIR`` (``/data`` in the image).
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

GARAGE_CONFIG = """\
metadata_dir = "{data}/meta"
data_dir = "{data}/data"
db_engine = "sqlite"
replication_factor = 1
rpc_bind_addr = "127.0.0.1:3901"
rpc_public_addr = "127.0.0.1:3901"
rpc_secret = "{rpc_secret}"

[s3_api]
s3_region = "garage"
api_bind_addr = "127.0.0.1:{port}"
root_domain = ".s3.garage.localhost"
"""


def log(message: str) -> None:
    print(f"[ergonaut up] {message}", file=sys.stderr, flush=True)


def data_dir() -> Path:
    path = Path(os.environ.get("DATA_DIR") or os.path.expanduser("~/.ergonaut"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def free_port(preferred: int) -> int:
    """``preferred`` if nothing listens on it, else any free local port."""
    import socket

    for candidate in (preferred, 0):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return sock.getsockname()[1]
    raise RuntimeError("no free port")


def wait_for(check, what: str, timeout: float = 30) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if check():
                return
        except Exception:  # noqa: BLE001 - keep polling until the deadline
            pass
        time.sleep(0.3)
    raise RuntimeError(f"{what} did not start within {timeout:.0f}s")


@dataclass
class Supervisor:
    env: dict = field(default_factory=lambda: dict(os.environ))
    procs: dict = field(default_factory=dict)
    keep: list = field(default_factory=list)

    def start(self, name: str, *args: str, **kwargs) -> subprocess.Popen:
        log(f"starting {name}: {' '.join(args)}")
        proc = subprocess.Popen(list(args), env=self.env, **kwargs)
        self.procs[name] = proc
        return proc

    def stop(self) -> None:
        for proc in self.procs.values():
            if proc.poll() is None:
                proc.terminate()
        end = time.monotonic() + 20
        for proc in self.procs.values():
            try:
                proc.wait(timeout=max(0.1, end - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.kill()
        for item in self.keep:
            stop = getattr(item, "cleanup", None)
            if stop:
                stop()

    def watch(self) -> int:
        while True:
            for name, proc in self.procs.items():
                code = proc.poll()
                if code is not None:
                    log(f"{name} exited with {code}; stopping everything")
                    return code or 1
            time.sleep(0.5)


# -- backing services ---------------------------------------------------------


def start_postgres(sup: Supervisor, data: Path) -> None:
    for name in ("LC_ALL", "LANG", "LC_CTYPE"):
        os.environ.setdefault(name, "C.UTF-8")
    import pgserver

    server = pgserver.get_server(str(data / "pgdata"), cleanup_mode="stop")
    sup.keep.append(server)
    server.psql("CREATE EXTENSION IF NOT EXISTS vector;")
    sup.env["DATABASE_URL"] = server.get_uri()
    log(f"embedded Postgres at {data / 'pgdata'}")


def start_redis(sup: Supervisor, data: Path) -> bool:
    binary = shutil.which("redis-server")
    if not binary:
        log("no CELERY_BROKER_URL and no redis-server: tasks run inline, no worker or beat")
        return False
    port = sup.env.get("ERGONAUT_REDIS_PORT") or str(free_port(6379))
    folder = data / "redis"
    folder.mkdir(exist_ok=True)
    sup.start(
        "redis",
        binary,
        "--bind", "127.0.0.1",
        "--port", port,
        "--dir", str(folder),
        "--appendonly", "yes",
        "--save", "",
    )  # fmt: skip
    url = f"redis://127.0.0.1:{port}/0"
    wait_for(lambda: _redis_ping(int(port)), "redis")
    sup.env["CELERY_BROKER_URL"] = url
    sup.env.setdefault("REDIS_URL", url)
    return True


def _redis_ping(port: int) -> bool:
    import socket

    with socket.create_connection(("127.0.0.1", port), timeout=1) as sock:
        sock.sendall(b"PING\r\n")
        return sock.recv(16).startswith(b"+PONG")


def start_garage(sup: Supervisor, data: Path) -> None:
    binary = shutil.which("garage")
    if not binary:
        log("no S3_ENDPOINT_URL and no garage: files are stored under DATA_DIR")
        sup.env.setdefault("MEDIA_ROOT", str(data / "media"))
        return
    folder = data / "garage"
    folder.mkdir(exist_ok=True)
    port = sup.env.get("ERGONAUT_GARAGE_PORT", "3900")
    state_file = folder / "ergonaut.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    if not state:
        state = {
            "rpc_secret": secrets.token_hex(32),
            "key_id": "GK" + secrets.token_hex(12),
            "secret": secrets.token_hex(32),
            "bucket": sup.env.get("AWS_STORAGE_BUCKET_NAME", "ergonaut-media"),
            "ready": False,
        }
        state_file.write_text(json.dumps(state))
        state_file.chmod(0o600)
    config = folder / "garage.toml"
    config.write_text(GARAGE_CONFIG.format(data=folder, rpc_secret=state["rpc_secret"], port=port))
    config.chmod(0o600)

    sup.env.setdefault("RUST_LOG", "garage=warn")
    sup.start("garage", binary, "-c", str(config), "server")

    def garage(*args: str) -> str:
        return subprocess.run([binary, "-c", str(config), *args], check=True, capture_output=True, text=True).stdout

    wait_for(lambda: garage("status"), "garage")
    if not state["ready"]:
        node = garage("node", "id", "-q").strip().split("@")[0]
        garage("layout", "assign", "-z", "local", "-c", "10G", node)
        garage("layout", "apply", "--version", "1")
        garage("key", "import", "--yes", "-n", "ergonaut", state["key_id"], state["secret"])
        garage("bucket", "create", state["bucket"])
        garage("bucket", "allow", "--read", "--write", "--owner", state["bucket"], "--key", "ergonaut")
        state["ready"] = True
        state_file.write_text(json.dumps(state))
    sup.env.update(
        S3_ENDPOINT_URL=f"http://127.0.0.1:{port}",
        AWS_ACCESS_KEY_ID=state["key_id"],
        AWS_SECRET_ACCESS_KEY=state["secret"],
        AWS_STORAGE_BUCKET_NAME=state["bucket"],
        AWS_S3_REGION_NAME="garage",
    )
    log(f"embedded Garage at {folder}, bucket {state['bucket']}")


# -- the app ------------------------------------------------------------------


def up(argv: list[str]) -> int:
    sup = Supervisor()
    data = data_dir()
    sup.env["DATA_DIR"] = str(data)
    python = sys.executable
    roles = set(argv) or {"web", "worker", "beat", "bots"}

    def handle(signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, handle)
    try:
        if not sup.env.get("DATABASE_URL"):
            start_postgres(sup, data)
        broker = bool(sup.env.get("CELERY_BROKER_URL")) or start_redis(sup, data)
        if not sup.env.get("S3_ENDPOINT_URL"):
            start_garage(sup, data)

        subprocess.run([python, "-m", "ergonaut.cli", "manage", "migrate", "--noinput"], env=sup.env, check=True)
        if "web" in roles:
            port = sup.env.get("PORT", "8000")
            sup.start("web", python, "-m", "uvicorn", "ergonaut.asgi:application", "--host", "0.0.0.0", "--port", port)
        if broker and "worker" in roles:
            sup.start("worker", python, "-m", "celery", "-A", "ergonaut", "worker", "-l", "info")
        if broker and "beat" in roles:
            schedule = str(data / "celerybeat-schedule")
            sup.start("beat", python, "-m", "celery", "-A", "ergonaut", "beat", "-l", "info", "-s", schedule)
        if "bots" in roles:
            sup.start("bots", python, "-m", "ergonaut.cli", "bots")
        return sup.watch()
    except KeyboardInterrupt:
        log("stopping")
        return 0
    finally:
        sup.stop()
