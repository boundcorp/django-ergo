from unittest.mock import patch

import pytest
from django.conf import settings
from django_ergo.settings import DEFAULTS
from django_ergo.settings import IMPORT_STRINGS
from django_ergo.settings import APISettings
from django_ergo.settings import import_from_string
from django_ergo.settings import perform_import
from django_ergo.settings import reload_api_settings


def test_perform_import():
    assert perform_import(None, "MY_SETTING") is None
    assert perform_import("django.conf.settings", "MY_SETTING") == settings
    assert perform_import(["django.conf.settings"], "MY_SETTING") == [settings]
    assert perform_import(123, "MY_SETTING") == 123


def test_import_from_string():
    assert import_from_string("django.conf.settings", "MY_SETTING") == settings
    with pytest.raises(ImportError):
        import_from_string("non.existent.module", "MY_SETTING")


@patch("django_ergo.settings.api_settings")
def test_reload_api_settings(mock_api_settings):
    # Arrange
    setting = "DJANGO_ERGO"
    kwargs = {"setting": setting}

    # Act
    reload_api_settings(**kwargs)

    # Assert
    mock_api_settings.reload.assert_called_once()


@pytest.fixture()
def api_settings():
    return APISettings(None, DEFAULTS, IMPORT_STRINGS)
