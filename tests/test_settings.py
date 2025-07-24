import pytest
from unittest.mock import patch

from django.conf import settings
from django.utils.module_loading import import_string

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
    assert perform_import(123, "MY_SETTING") == 123  # noqa: PLR2004


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


@pytest.mark.django_db()
class TestAPISettings:

    def test_api_settings(self, api_settings):
        assert api_settings.MY_MODEL == import_string("django_ergo.models.MyModel")

    # def test_override_settings(self, api_settings):
    #     from django.test import override_settings
    #     with override_settings(
    #         DJANGO_PACKAGE_BOILERPLATE={"FOO": "django.conf.settings"}
    #     ):
    #         assert api_settings.FOO == settings

    def test_user_setting(self):
        api_settings = APISettings({"FOO": "newvalue"})
        assert api_settings.FOO == "newvalue"

    def test_non_existent_setting(self, api_settings):
        with pytest.raises(AttributeError):
            api_settings.NON_EXISTENT_SETTING  # noqa: B018

    def test_removed_setting(self):
        with pytest.raises(RuntimeError):
            APISettings({"REMOVED_SETTING": "value"})

    def test_user_settings(self, api_settings):
        assert api_settings.user_settings == {"FOO": "newvalue"}

    def test_reload(self, api_settings):
        api_settings.reload()
        assert "MY_MODEL" not in api_settings._cached_attrs  # noqa: SLF001

    # def test_setting_changed(self, api_settings):
    #     settings.DJANGO_PACKAGE_BOILERPLATE = {"FOO": "new_value"}
    #     setting_changed.send(None, setting="FOO", value="new_value", enter=True)
    #     assert api_settings.FOO == "new_value"
