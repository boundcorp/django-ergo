import shutil
import sys

import pytest

from ergonaut import up


def test_without_redis_or_garage_it_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(up.shutil, "which", lambda name: None)
    sup = up.Supervisor(env={})
    assert up.start_redis(sup, tmp_path) is False
    up.start_garage(sup, tmp_path)
    assert "CELERY_BROKER_URL" not in sup.env
    assert "S3_ENDPOINT_URL" not in sup.env
    assert sup.env["MEDIA_ROOT"] == str(tmp_path / "media")


@pytest.mark.skipif(not shutil.which("redis-server"), reason="redis-server not installed")
def test_starts_redis_and_points_celery_at_it(tmp_path):
    sup = up.Supervisor(env={"ERGONAUT_REDIS_PORT": "6391"})
    try:
        assert up.start_redis(sup, tmp_path) is True
        assert sup.env["CELERY_BROKER_URL"] == "redis://127.0.0.1:6391/0"
        assert up._redis_ping(6391)
    finally:
        sup.stop()


def test_watch_stops_when_a_process_exits():
    sup = up.Supervisor()
    sup.start("sleeper", sys.executable, "-c", "import time; time.sleep(30)")
    sup.start("quitter", sys.executable, "-c", "raise SystemExit(3)")
    try:
        assert sup.watch() == 3
    finally:
        sup.stop()
    assert sup.procs["sleeper"].poll() is not None


def test_free_port_skips_a_busy_one():
    import socket

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        assert up.free_port(taken) != taken
