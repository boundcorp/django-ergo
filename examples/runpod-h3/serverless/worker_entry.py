"""Historical Serverless worker from the 2026-11-13 trial; BACKUP ONLY.

Not a complete deployment: independent, approved deadline and cleanup controls
are required. Do not run without the approval described in README.md.
"""

# Historical trial code preserved without changing its runtime behavior.
# ruff: noqa: BLE001, PLW1510, PTH113, PTH123, PTH208, S603, S607

import http.server
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request

T = time.time()


def mark(tag, **kw):
    print(
        "ERGO_SL "
        + tag
        + " "
        + json.dumps({"t": time.time(), "elapsed": round(time.time() - T, 1), **kw}),
        flush=True,
    )


def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


mark(
    "BASELINE",
    worker=os.getenv("RUNPOD_POD_ID"),
    dc=os.getenv("RUNPOD_DC_ID"),
    cgroup_mem_max=read("/sys/fs/cgroup/memory.max"),
    gpu=subprocess.run(
        ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
        capture_output=True,
        text=True,
    ).stdout.strip(),
    disk_free=shutil.disk_usage("/").free,
)
limit = read("/sys/fs/cgroup/memory.max")
if limit and limit != "max" and int(limit) < 249000000000:
    mark("INSUFFICIENT_RAM", limit=limit)
    raise SystemExit(1)


class Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:30010/health", timeout=2
            ) as r:
                ready = json.load(r).get("status") == "ok"
        except Exception:
            ready = False
        self.send_response(200 if ready else 204)
        self.end_headers()

    def log_message(self, *args):
        pass


threading.Thread(
    target=http.server.ThreadingHTTPServer(("0.0.0.0", 8080), Health).serve_forever,
    daemon=True,
).start()
mark("DOWNLOAD_START")
t = time.time()
rc = subprocess.call(
    [
        "hf",
        "download",
        "MiniMaxAI/MiniMax-H3",
        "--revision",
        "42ed227ee7df40d41602854ae760620d6eb651fe",
        "--include",
        "FL2VA/*",
        "--local-dir",
        "/workspace/model",
    ]
)
mark("DOWNLOAD_END", rc=rc, seconds=time.time() - t)
if rc:
    raise SystemExit(rc)
model = "/workspace/model/FL2VA"
vv = model + "/video_vae"
if os.path.isfile(vv + "/source/model.safetensors") and not any(
    x.endswith(".safetensors") for x in os.listdir(vv)
):
    os.symlink("source/model.safetensors", vv + "/model.safetensors")
mark("SGLANG_START")
rc = subprocess.call(
    [
        "sglang",
        "serve",
        "--model-path",
        model,
        "--num-gpus",
        "1",
        "--tp-size",
        "1",
        "--ulysses-degree",
        "1",
        "--quantization",
        "kitchen_int8",
        "--attention-backend",
        "fa",
        "--performance-mode",
        "memory",
        "--layerwise-offload-components",
        "dit,text_encoder",
        "--dit-offload-prefetch-size",
        "1",
        "--dit-layerwise-resident-layers",
        "0",
        "--enable-torch-compile",
        "false",
        "--host",
        "0.0.0.0",
        "--port",
        "30010",
    ],
    env={**os.environ, "HF_HUB_OFFLINE": "1"},
)
mark("SGLANG_EXIT", rc=rc)
raise SystemExit(rc or 1)
