"""CPU-only contracts for restart-safe SSH and the immutable-image override."""

import ast
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from django_ergo.plugins.runpod.ssh import SSH_BOOTSTRAP


def statements():
    return ast.parse(SSH_BOOTSTRAP).body


def execute(nodes, namespace):
    exec(
        compile(ast.Module(body=nodes, type_ignores=[]), "bootstrap-fixture", "exec"),
        namespace,
    )


def test_bootstrap_reuses_attempt_key_and_replaces_unmarked_overlay(tmp_path):
    private = tmp_path / "ergo_host_key"
    public = tmp_path / "ergo_host_key.pub"
    for path in (private, public):
        path.write_text("old overlay key")
    nodes = statements()
    start = next(
        index
        for index, node in enumerate(nodes)
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "key_path"
    )
    end = next(
        index
        for index, node in enumerate(nodes)
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "config"
    )

    def run(argv, capture=False):
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,
            check=True,
        )
        return result.stdout if capture else None

    environ = {"ERGO_ATTEMPT_FINGERPRINT": "attempt-one"}
    namespace = {
        "Path": lambda path: tmp_path / Path(path).name,
        "run": run,
        "os": SimpleNamespace(environ=environ),
    }
    execute(nodes[start:end], namespace)
    first = public.read_text()
    public.unlink()
    execute(nodes[start:end], namespace)
    assert public.read_text() == first  # same pin, public file recovered
    environ["ERGO_ATTEMPT_FINGERPRINT"] = "attempt-two"
    execute(nodes[start:end], namespace)
    assert public.read_text() != first  # never inherit another attempt's key
    assert private.stat().st_mode & 0o077 == 0


def test_launcher_exit_keeps_ssh_and_failure_marker_until_original_cutoff(tmp_path):
    nodes = statements()
    index = next(
        index
        for index, node in enumerate(nodes)
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "launcher"
    )
    child = Mock()
    child.wait.return_value = 42
    spawn = Mock(return_value=child)
    ssh_server = Mock()
    ssh_server.poll.return_value = None
    clock = [100.0]

    def sleep(seconds):
        clock[0] += seconds

    stop = Mock(side_effect=SystemExit(124))
    namespace = {
        "Path": lambda path: tmp_path / Path(path).name,
        "subprocess": SimpleNamespace(Popen=spawn),
        "time": SimpleNamespace(time=lambda: clock[0], sleep=sleep),
        "os": SimpleNamespace(environ={"ERGO_ATTEMPT_FINGERPRINT": "approved"}),
        "json": json,
        "ssh_server": ssh_server,
        "cutoff": 103.0,
        "deadline": 223.0,
        "stop": stop,
    }
    with pytest.raises(SystemExit) as error:
        execute(nodes[index:], namespace)
    assert error.value.code == 124
    assert clock[0] == 103.0
    assert spawn.call_count == 1
    state = json.loads((tmp_path / "h3-launch-state.json").read_text())
    assert state["fingerprint"] == "approved"
    assert state["state"] == "failed"
    assert state["returncode"] == 42
    assert state["observed"] == 100.0
    assert state["deadline"] == 223.0
    ssh_server.kill.assert_not_called()
    ssh_server.terminate.assert_not_called()
    stop.assert_called_once_with()
