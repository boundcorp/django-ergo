import importlib

from ergonaut.settings import project


def test_lifecycle_events_environment_toggle_is_opt_in(monkeypatch):
    key = "ERGONAUT_LIFECYCLE_EVENTS"
    try:
        with monkeypatch.context() as env:
            env.delenv(key, raising=False)
            assert importlib.reload(project).DJANGO_ERGO["LIFECYCLE_EVENTS_ENABLED"] is False

            for value in ("1", "true", "yes", "on", "TrUe", "YES", "ON"):
                env.setenv(key, value)
                assert importlib.reload(project).DJANGO_ERGO["LIFECYCLE_EVENTS_ENABLED"] is True

            for value in ("", "0", "false", "enabled"):
                env.setenv(key, value)
                assert importlib.reload(project).DJANGO_ERGO["LIFECYCLE_EVENTS_ENABLED"] is False
    finally:
        importlib.reload(project)
