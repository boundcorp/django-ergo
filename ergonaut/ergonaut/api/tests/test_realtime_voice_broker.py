"""Offline bridge transport probe against an explicitly supplied isolated Redis."""

import os
import subprocess

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
