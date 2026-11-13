"""CPU-only tests of the prepared image entrypoint's early-stop behavior."""

import importlib.util
import json
import signal
import time
from pathlib import Path
from unittest.mock import Mock

import pytest


def launcher():
    path = Path(__file__).resolve().parents[1] / "examples/runpod-h3/launch.py"
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
