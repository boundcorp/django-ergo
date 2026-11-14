"""Offline bridge transport probe against an explicitly supplied isolated Redis."""

import os
import subprocess
import sys

import pytest


def test_isolated_bridge_can_publish_and_consume_a_celery_message():
    broker = os.environ.get("VOICE_TEST_BROKER_URL")
    python = os.environ.get("VOICE_TEST_BRIDGE_PYTHON")
    if not broker or not python:
        pytest.skip("set VOICE_TEST_BROKER_URL and VOICE_TEST_BRIDGE_PYTHON for isolated broker integration")
    assert broker.startswith("redis://127.0.0.1:")
    subprocess.run(
        [
            python,
            "-c",
            "import sys; from ergonaut.realtime_voice_preflight import check_broker; check_broker(sys.argv[1])",
            broker,
        ],
        check=True,
        timeout=30,
    )


@pytest.mark.parametrize("module", ["ergonaut.realtime_voice_preflight", "ergonaut.realtime_voice_local"])
def test_retired_paid_flag_cannot_launch_the_native_cost_profile(tmp_path, module):
    # No real credentials/configuration are reachable even if argument handling regresses.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OPENAI_", "LIVEKIT_", "ERGO_VOICE_")) and key != "KUBECONFIG"
    }
    environment["HOME"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", module, "--approve-paid"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2
