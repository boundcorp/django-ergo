"""Pinned H3 launcher, also delivered verbatim by the SSH entrypoint override.

Stdlib only: the reviewed image can run this file without installing django-ergo.
The independent off-Pod cleaner still owns billing cleanup.
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
STDERR_LIMIT = 64 * 1024


def _stderr_tail(path):
    """Bound failure evidence; provider management credentials never enter H3."""
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            offset = max(0, stream.tell() - STDERR_LIMIT)
            stream.seek(offset)
            text = stream.read().decode("utf-8", errors="replace")
            if offset:
                # Drop a cut line rather than retain a partial token or UTF-8
                # prefix at the bounded read boundary.
                text = text.partition("\n")[2]
    except OSError:
        return ""
    for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        if token := os.environ.get(name):
            text = text.replace(token, "[REDACTED]")
    return text


def main():  # noqa: C901, PLR0912, PLR0915 — ordered download/startup/serve lifecycle.
    started = time.time()
    deadline = float(os.environ["ERGO_DEADLINE"])
    # Clock is established before Pod creation. Never reset it on restart.
    if not started < deadline <= started + 3600:
        message = "existing absolute deadline must be within one hour"
        raise ValueError(message)
    cutoff = deadline - 120
    if started >= cutoff:
        message = "work cutoff reached before launcher"
        raise ValueError(message)
    work = Path("/workspace")
    model = work / "model"
    timing_path = work / "h3-timings.json"
    state_path = work / "h3-launch-state.json"
    stderr_path = work / "h3-server-stderr.log"
    fingerprint = os.environ.get("ERGO_ATTEMPT_FINGERPRINT", "")
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
    server_stderr = None
    stage = "download"

    def persist():
        temporary = timing_path.with_suffix(".next")
        temporary.write_text(json.dumps(timings, indent=2))
        temporary.replace(timing_path)

    def publish(state, reason=None, returncode=None):
        record = {
            "fingerprint": fingerprint,
            "state": state,
            "stage": stage,
            "reason": reason,
            "returncode": returncode,
            "observed": time.time(),
            "deadline": deadline,
        }
        if state == "failed":
            record["timings"] = timings
            if stage in ("startup", "serve"):
                record["stderr_tail"] = _stderr_tail(stderr_path)
        temporary = state_path.with_suffix(".next")
        temporary.write_text(json.dumps(record))
        temporary.replace(state_path)
        if state == "failed":
            print("ERGO_H3_FAILURE " + json.dumps(record), flush=True)  # noqa: T201 -- provider log marker

    def stop(_signal=None, _frame=None):
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        publish("failed", "absolute work cutoff or launcher cancellation", 124)
        persist()
        raise SystemExit(124)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGALRM, stop)
    signal.setitimer(signal.ITIMER_REAL, max(0.01, cutoff - time.time()))
    # A provider/container restart must not replay a failed paid attempt.
    if state_path.exists():
        previous = json.loads(state_path.read_text())
        if previous.get("fingerprint") == fingerprint:
            message = "launcher already attempted; no automatic restart"
            if previous.get("state") != "failed":
                stage = previous.get("stage", stage)
                publish("failed", message)
            raise RuntimeError(message)
    publish("starting")
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
                "FL2VA/*",
                "--local-dir",
                str(model),
            ],
            start_new_session=True,
        )
        code = child.wait()
        if code != 0:
            message = "checkpoint download failed; no retry or replacement"
            raise RuntimeError(message)  # noqa: TRY301 -- publish terminal state below
        timings["download_seconds"] = time.time() - download_started
        persist()
        loading = time.time()
        stage = "startup"
        publish("starting")
        env = {**os.environ, "HF_HUB_OFFLINE": "1"}
        # Keep the model traceback available through SSH even after the child
        # exits. The bounded tail is copied into terminal state before cleanup.
        server_stderr = os.fdopen(
            os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb"
        )
        child = subprocess.Popen(
            [
                "sglang",
                "serve",
                "--model-path",
                # Registry validates this index before variant handling. Passing
                # --model-variant would append FL2VA a second time in _load_config.
                str(model / "FL2VA"),
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
            stderr=server_stderr,
            start_new_session=True,
        )
        while child.poll() is None:
            try:
                with urlopen("http://127.0.0.1:30010/health", timeout=2) as response:
                    ready = json.load(response).get("status") == "ok"
                if ready:
                    timings["startup_seconds"] = time.time() - loading
                    stage = "serve"
                    publish("ready")
                    persist()
                    break
            except OSError:
                pass  # Readiness GET only; never repeat generation or rental.
            time.sleep(1)
        code = child.wait()
        # Even an exit code of zero is terminal: this is a persistent service.
        message = "SGLang stopped; no automatic restart"
        raise RuntimeError(message)  # noqa: TRY301 -- publish terminal state below
    except Exception as error:
        publish("failed", str(error), child.poll() if child is not None else None)
        raise
    finally:
        persist()
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        if server_stderr is not None:
            server_stderr.close()


if __name__ == "__main__":
    main()
