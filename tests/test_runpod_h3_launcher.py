"""CPU-only tests of the prepared image entrypoint's early-stop behavior."""

import importlib.util
import json
import signal
import time
from pathlib import Path
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
    download_argv = spawn.call_args_list[0].args[0]
    assert download_argv[download_argv.index("--include") + 1] == "FL2VA/*"
    assert "model_index.json" not in download_argv
    assert download_argv[download_argv.index("--revision") + 1] == module.MODEL_REVISION
    serve_argv = spawn.call_args_list[1].args[0]
    assert serve_argv[serve_argv.index("--model-path") + 1] == str(
        tmp_path / "model/FL2VA"
    )
    assert "--model-variant" not in serve_argv
    assert "--model-subfolder" not in serve_argv
    assert spawn.call_args.kwargs["env"]["HF_HUB_OFFLINE"] == "1"
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
