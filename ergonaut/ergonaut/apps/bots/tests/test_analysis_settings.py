"""Production settings mapping and the registered daily maintenance task."""

import importlib

import pytest
from celery.schedules import crontab
from django.core.exceptions import ImproperlyConfigured

from ergonaut.settings import project

ENV_KEYS = (
    "ERGO_ANALYSIS_ENABLED",
    "ERGO_ANALYSIS_CONTROLLER_URL",
    "ERGO_ANALYSIS_CONTROLLER_KEY",
    "ERGO_ANALYSIS_CONTROLLER_CA_FILE",
    "ERGO_ANALYSIS_RETENTION_DAYS",
)


@pytest.fixture
def production_environment(monkeypatch):
    # Reload the real production module without creating another embedded server.
    # Never contact this deliberately non-routable fixture database.
    original = vars(project).copy()
    with monkeypatch.context() as env:
        for key in ENV_KEYS:
            env.delenv(key, raising=False)
        env.setenv("DATABASE_URL", "postgresql://fixture@database.invalid/fixture")
        env.setenv("APP_ENV", "production")
        # Settings uses this flag to select embedded PG under pytest. Temporarily
        # remove it while reloading; DATABASE_URL above keeps production isolated.
        env.delenv("ERGO_TEST_ISOLATED", raising=False)
        yield env
    # Restore the module, including after a failed reload, without starting PG.
    vars(project).clear()
    vars(project).update(original)


def test_production_analysis_default_off(production_environment):
    config = importlib.reload(project)
    assert config.ERGO_ANALYSIS_ENABLED is False
    assert config.ERGO_ANALYSIS_CONTROLLER_URL == ""
    assert config.ERGO_ANALYSIS_CONTROLLER_KEY == b""
    assert config.ERGO_ANALYSIS_CONTROLLER_CA_FILE == ""
    assert config.ERGO_ANALYSIS_RETENTION_DAYS == 30
    assert "cleanup-analysis" not in config.CELERY_BEAT_SCHEDULE


@pytest.mark.parametrize(
    "value,enabled", [("false", False), ("FALSE", False), ("0", False), ("true", True), ("1", True)]
)
def test_production_analysis_environment_mapping(production_environment, value, enabled):
    env = production_environment
    env.setenv("ERGO_ANALYSIS_ENABLED", value)
    env.setenv("ERGO_ANALYSIS_CONTROLLER_URL", "https://controller.invalid")
    env.setenv("ERGO_ANALYSIS_CONTROLLER_KEY", "fixture-only-key-32-bytes-long-é!!")
    env.setenv("ERGO_ANALYSIS_CONTROLLER_CA_FILE", "/fixture/unmounted-ca.pem")
    env.setenv("ERGO_ANALYSIS_RETENTION_DAYS", "365")
    config = importlib.reload(project)
    assert config.ERGO_ANALYSIS_ENABLED is enabled
    assert config.ERGO_ANALYSIS_CONTROLLER_URL == "https://controller.invalid"
    assert config.ERGO_ANALYSIS_CONTROLLER_KEY == "fixture-only-key-32-bytes-long-é!!".encode()
    assert config.ERGO_ANALYSIS_CONTROLLER_CA_FILE == "/fixture/unmounted-ca.pem"
    assert config.ERGO_ANALYSIS_RETENTION_DAYS == 365
    if enabled:
        assert config.CELERY_BEAT_SCHEDULE["cleanup-analysis"] == {
            "task": "ergonaut.cleanup_analysis",
            "schedule": crontab(hour=3, minute=0),
        }
    else:
        assert "cleanup-analysis" not in config.CELERY_BEAT_SCHEDULE


@pytest.mark.parametrize(
    "key,value",
    [
        ("ERGO_ANALYSIS_ENABLED", "fixture-sensitive"),
        ("ERGO_ANALYSIS_RETENTION_DAYS", "0"),
        ("ERGO_ANALYSIS_RETENTION_DAYS", "366"),
        ("ERGO_ANALYSIS_RETENTION_DAYS", "fixture-sensitive"),
    ],
)
def test_production_invalid_analysis_environment(production_environment, key, value):
    production_environment.setenv(key, value)
    with pytest.raises(ImproperlyConfigured, match=key) as error:
        importlib.reload(project)
    assert "fixture-sensitive" not in str(error.value)


def test_daily_task_uses_guarded_management_command(monkeypatch, settings):
    from ergonaut.apps.bots.tasks import cleanup_analysis

    settings.ERGO_ANALYSIS_ENABLED = False
    monkeypatch.setattr(
        "django_ergo.management.commands.cleanup_analysis.configured_client",
        lambda: pytest.fail("Disabled task contacted controller"),
    )
    assert cleanup_analysis.name == "ergonaut.cleanup_analysis"
    cleanup_analysis.run()
