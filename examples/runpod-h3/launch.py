"""Prepared image entrypoint; run only after launch authorization.

No provider credential is needed or accepted. The independent off-Pod cleaner
still owns billing cleanup; killing local children alone does not end billing.
"""

# Fixed model/runtime executables; offline image smoke never downloads weights.
# ruff: noqa: S603, S607
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen

MODEL_REVISION = "42ed227ee7df40d41602854ae760620d6eb651fe"
SGLANG_REVISION = "2511743bd784e69e5a81ca3d926a000711dae4ab"


def main():  # noqa: C901, PLR0915 — ordered download/startup/serve lifecycle.
    started = time.time()
    deadline = float(os.environ["ERGO_DEADLINE"])
    # Clock is established before Pod creation. Never reset it on container restart.
    if not started < deadline <= started + 3600:
        message = "existing absolute deadline must be within one hour"
        raise ValueError(message)
    cutoff = deadline - 120
    work = Path("/workspace")
    model = work / "model"
    timing_path = work / "h3-timings.json"
    timings = {
        "model_revision": MODEL_REVISION,
        "sglang_revision": SGLANG_REVISION,
        "precision": "kitchen_int8",
        "compile_seconds": 0,
        "compile_reason": "enable-torch-compile=false",
        "started": started,
        "deadline": deadline,
        "download_seconds": None,
        "startup_seconds": None,
    }
    child = None

    def persist():
        temporary = timing_path.with_suffix(".next")
        temporary.write_text(json.dumps(timings, indent=2))
        temporary.replace(timing_path)

    def stop(_signal=None, _frame=None):
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        persist()
        raise SystemExit(124)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGALRM, stop)
    signal.setitimer(signal.ITIMER_REAL, max(0.01, cutoff - time.time()))
    persist()
    try:
        download_started = time.time()
        child = subprocess.Popen(
            [
                "hf",
                "download",
                "MiniMaxAI/MiniMax-H3",
                "--revision",
                MODEL_REVISION,
                "--include",
                "model_index.json",
                "--include",
                "FL2VA/*",
                "--local-dir",
                str(model),
            ],
            start_new_session=True,
        )
        if child.wait() != 0:
            message = "checkpoint download failed; no retry or replacement"
            raise RuntimeError(message)
        timings["download_seconds"] = time.time() - download_started
        persist()
        loading = time.time()
        env = {**os.environ, "HF_HUB_OFFLINE": "1"}
        child = subprocess.Popen(
            [
                "sglang",
                "serve",
                "--model-path",
                str(model),
                "--model-variant",
                "fl2va",
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
                "127.0.0.1",
                "--port",
                "30010",
            ],
            env=env,
            start_new_session=True,
        )
        while child.poll() is None:
            try:
                with urlopen("http://127.0.0.1:30010/health", timeout=2) as response:
                    ready = json.load(response).get("status") == "ok"
                if ready:
                    timings["startup_seconds"] = time.time() - loading
                    persist()
                    break
            except OSError:
                pass  # Readiness GET only; never repeat generation or rental.
            time.sleep(1)
        if child.wait() != 0:
            message = "SGLang stopped; no automatic restart"
            raise RuntimeError(message)
    finally:
        persist()
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()


if __name__ == "__main__":
    main()
