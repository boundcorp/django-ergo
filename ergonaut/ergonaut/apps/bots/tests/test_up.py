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


def test_other_commands_use_the_services_a_running_up_started(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgres://mine")
    for name in ("CELERY_BROKER_URL", "REDIS_URL"):
        monkeypatch.setenv(name, "")  # so monkeypatch puts back what attach() sets
        monkeypatch.delenv(name)
    sup = up.Supervisor()
    sup.env["CELERY_BROKER_URL"] = "redis://127.0.0.1:6400/0"
    sup.env["REDIS_URL"] = "redis://127.0.0.1:6400/0"
    sup.env["DATABASE_URL"] = "postgres://up"
    up.write_state(sup, tmp_path)

    assert sorted(up.attach()) == ["CELERY_BROKER_URL", "REDIS_URL"]
    assert up.os.environ["CELERY_BROKER_URL"] == "redis://127.0.0.1:6400/0"
    assert up.os.environ["DATABASE_URL"] == "postgres://mine"  # what this process sets wins
    assert (tmp_path / up.STATE_FILE).stat().st_mode & 0o077 == 0

    up.clear_state(tmp_path)
    assert not (tmp_path / up.STATE_FILE).exists()


def test_a_stopped_up_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CELERY_BROKER_URL", "")
    monkeypatch.delenv("CELERY_BROKER_URL")
    (tmp_path / up.STATE_FILE).write_text('{"pid": 999999999, "env": {"CELERY_BROKER_URL": "redis://x"}}')

    assert up.attach() == []
    assert "CELERY_BROKER_URL" not in up.os.environ
