"""CPU-only tests of the prepared image entrypoint's early-stop behavior."""

import argparse
import importlib.util
import json
import signal
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def launcher():
    path = (
        Path(__file__).resolve().parents[1]
        / "src/django_ergo/plugins/runpod/h3_launcher.py"
    )
    spec = importlib.util.spec_from_file_location("h3_launcher_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_expired_deadline_refuses_download_or_server(monkeypatch):
    module = launcher()
    monkeypatch.setenv("ERGO_DEADLINE", str(time.time() - 1))
    spawn = Mock()
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    with pytest.raises(ValueError, match="absolute deadline"):
        module.main()
    spawn.assert_not_called()


def test_work_cutoff_refuses_download_even_before_rental_deadline(monkeypatch):
    module = launcher()
    monkeypatch.setenv("ERGO_DEADLINE", str(time.time() + 60))
    spawn = Mock()
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    with pytest.raises(ValueError, match="work cutoff"):
        module.main()
    spawn.assert_not_called()


def test_cutoff_during_download_kills_child_without_starting_model(
    tmp_path, monkeypatch
):
    module = launcher()
    monkeypatch.setenv("ERGO_DEADLINE", str(time.time() + 300))
    monkeypatch.setattr(module, "Path", lambda _path: tmp_path)
    handlers = {}
    monkeypatch.setattr(
        module.signal, "signal", lambda signum, fn: handlers.update({signum: fn})
    )
    timer = Mock()
    monkeypatch.setattr(module.signal, "setitimer", timer)
    child = Mock(pid=98765)
    child.poll.return_value = None
    waits = 0

    def wait():
        nonlocal waits
        waits += 1
        if waits == 1:
            handlers[signal.SIGALRM]()
        child.poll.return_value = -9
        return -9

    child.wait.side_effect = wait
    spawn = Mock(return_value=child)
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    kill = Mock()
    monkeypatch.setattr(module.os, "killpg", kill)
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 124
    assert spawn.call_count == 1
    assert kill.call_args.args[0] == 98765
    timing = json.loads((tmp_path / "h3-timings.json").read_text())
    assert timing["download_seconds"] is None
    assert timing["startup_seconds"] is None
    assert 0 < timer.call_args.args[1] < 181


@pytest.mark.parametrize("returncode", [0, 1, -9])
def test_server_exit_is_immediate_and_fingerprint_bound(
    tmp_path, monkeypatch, returncode
):
    module = launcher()
    monkeypatch.setenv("ERGO_DEADLINE", str(time.time() + 300))
    monkeypatch.setenv("ERGO_ATTEMPT_FINGERPRINT", "approved-attempt")
    monkeypatch.setattr(module, "Path", lambda _path: tmp_path)
    monkeypatch.setattr(module.signal, "signal", Mock())
    monkeypatch.setattr(module.signal, "setitimer", Mock())
    download = Mock()
    download.wait.return_value = 0
    server = Mock()
    server.poll.return_value = returncode
    server.wait.return_value = returncode
    spawn = Mock(side_effect=[download, server])
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    sleep = Mock()
    monkeypatch.setattr(module.time, "sleep", sleep)
    health = Mock()
    monkeypatch.setattr(module, "urlopen", health)
    with pytest.raises(RuntimeError, match="SGLang stopped"):
        module.main()
    sleep.assert_not_called()
    health.assert_not_called()
    state = json.loads((tmp_path / "h3-launch-state.json").read_text())
    assert state["fingerprint"] == "approved-attempt"
    assert state["state"] == "failed"
    assert state["stage"] == "startup"
    assert state["returncode"] == returncode
    assert spawn.call_count == 2
    # Restarting the same container may expose SSH/status, never redownload.
    with pytest.raises(RuntimeError, match="already attempted"):
        module.main()
    assert spawn.call_count == 2


def test_download_failure_is_observable_without_server_start(tmp_path, monkeypatch):
    module = launcher()
    monkeypatch.setenv("ERGO_DEADLINE", str(time.time() + 300))
    monkeypatch.setenv("ERGO_ATTEMPT_FINGERPRINT", "approved-attempt")
    monkeypatch.setattr(module, "Path", lambda _path: tmp_path)
    monkeypatch.setattr(module.signal, "signal", Mock())
    monkeypatch.setattr(module.signal, "setitimer", Mock())
    child = Mock()
    child.wait.return_value = 7
    child.poll.return_value = 7
    spawn = Mock(return_value=child)
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    with pytest.raises(RuntimeError, match="checkpoint download failed"):
        module.main()
    assert spawn.call_count == 1
    state = json.loads((tmp_path / "h3-launch-state.json").read_text())
    assert state["state"] == "failed"
    assert state["stage"] == "download"
    assert state["returncode"] == 7


def test_ready_service_exit_also_fails_without_restart(tmp_path, monkeypatch):
    module = launcher()
    monkeypatch.setenv("ERGO_DEADLINE", str(time.time() + 300))
    monkeypatch.setenv("ERGO_ATTEMPT_FINGERPRINT", "approved-attempt")
    monkeypatch.setattr(module, "Path", lambda _path: tmp_path)
    monkeypatch.setattr(module.signal, "signal", Mock())
    monkeypatch.setattr(module.signal, "setitimer", Mock())
    download = Mock()
    download.wait.return_value = 0
    server = Mock()
    server.poll.side_effect = [None, 1, 1]
    server.wait.return_value = 1
    spawn = Mock(side_effect=[download, server])
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    response = Mock()
    response.read.return_value = b'{"status": "ok"}'
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(module, "urlopen", Mock(return_value=response))
    with pytest.raises(RuntimeError, match="SGLang stopped"):
        module.main()
    state = json.loads((tmp_path / "h3-launch-state.json").read_text())
    assert state["state"] == "failed"
    assert state["stage"] == "serve"
    timing = json.loads((tmp_path / "h3-timings.json").read_text())
    assert timing["startup_seconds"] is not None
    assert spawn.call_count == 2


def test_bounded_stderr_tail_keeps_traceback_and_redacts_hf_credentials(
    tmp_path, monkeypatch
):
    module = launcher()
    token = "hf_private_fixture_token"
    monkeypatch.setenv("HF_TOKEN", token)
    path = tmp_path / "stderr.log"
    trace = (
        f"Traceback (most recent call last):\nRuntimeError: bad checkpoint {token}\n"
    )
    path.write_text("old noise" * module.STDERR_LIMIT + "\n" + trace)
    tail = module._stderr_tail(path)
    assert tail == trace.replace(token, "[REDACTED]")
    assert len(tail.encode()) <= module.STDERR_LIMIT
    path.unlink()
    assert module._stderr_tail(path) == ""


def test_frozen_partition_loads_with_nested_video_vae_weights(tmp_path, monkeypatch):
    """Run the launcher's arguments through the pinned consumer's local loader."""
    fixture_dir = Path(__file__).parent / "fixtures"
    layout = json.loads((fixture_dir / "runpod_h3_fl2va_layout.json").read_text())
    consumer = {}
    source = fixture_dir / "sglang_h3_local_loader.txt"
    exec(compile(source.read_text(), str(source), "exec"), consumer)

    def local_download(path, *, force_diffusers_model=False, **_kwargs):
        # The real runtime falls through to the Hub when this local check fails.
        # No network boundary is permitted in this CPU-only regression.
        if not Path(path).is_dir() or (
            force_diffusers_model
            and not consumer["_verify_diffusers_model_complete"](path)
        ):
            message = "Pinned SGLang rejects this local model as incomplete"
            raise ValueError(message)
        return path

    consumer["maybe_download_model"] = local_download
    module = launcher()
    monkeypatch.setenv("ERGO_DEADLINE", str(time.time() + 300))
    monkeypatch.setattr(module, "Path", lambda _path: tmp_path)
    monkeypatch.setattr(module.signal, "signal", Mock())
    monkeypatch.setattr(module.signal, "setitimer", Mock())
    accepted = {}

    def spawn(argv, **_kwargs):
        parser = argparse.ArgumentParser()
        if argv[0] == "hf":
            parser.add_argument("--local-dir", type=Path)
            options, _ = parser.parse_known_args(argv)
            # HF local_dir retains repository-relative paths; no real weights.
            for name in layout["files"]:
                path = options.local_dir / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            index = options.local_dir / "FL2VA/model_index.json"
            index.write_text(json.dumps(layout["model_index"]))
        else:
            parser.add_argument("--model-path")
            parser.add_argument("--model-subfolder")
            options, _ = parser.parse_known_args(argv)
            pipeline = SimpleNamespace(
                model_path=options.model_path,
                server_args=SimpleNamespace(
                    model_subfolder=options.model_subfolder,
                    revision=None,
                ),
                default_model_subfolder="FL2VA",
            )
            accepted["generic_complete"] = consumer["_verify_diffusers_model_complete"](
                pipeline.model_path
            )
            accepted["config"] = consumer["_load_config"](pipeline)
            accepted["model_path"] = Path(pipeline.model_path).resolve()
        child = Mock()
        child.wait.return_value = 0
        child.poll.return_value = 0
        return child

    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    with pytest.raises(RuntimeError, match="SGLang stopped"):
        module.main()
    # The generic completeness gate passes, so the local partition is never
    # retried as a Hub repository id.
    assert accepted["generic_complete"] is True
    assert accepted["config"] == layout["model_index"]
    assert accepted["model_path"] == tmp_path / "model/FL2VA"
    vae = accepted["model_path"] / "video_vae"
    # The link aliases the one shipped VAE file inside the partition, no copy.
    assert (vae / "model.safetensors").resolve() == vae / "source/model.safetensors"
    assert (vae / "source/model.safetensors").is_file()
