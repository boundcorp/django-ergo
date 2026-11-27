"""
Pytest configuration for OpenAI testing.
"""


def pytest_configure(config):
    """Configure pytest markers."""
    config.addinivalue_line("markers", "openai: mark test as OpenAI integration test")
    config.addinivalue_line(
        "markers", "openai_real: mark test as requiring real OpenAI API"
    )
    config.addinivalue_line(
        "markers", "openai_mocked: mark test as using mocked OpenAI API"
    )
    config.addinivalue_line("markers", "slow: mark test as slow")
    config.addinivalue_line("markers", "integration: mark test as integration test")


import pytest  # noqa: E402

DB_FIXTURES = {"db", "transactional_db", "django_db_reset_sequences"}


@pytest.fixture(autouse=True)
def _provider_switches_without_db(request, monkeypatch):
    """Runtime provider switches (bots.pools) live in the database. Tests that
    don't use it read none, as a fresh deployment has none."""
    if request.node.get_closest_marker("django_db") is None and not (
        DB_FIXTURES & set(request.fixturenames)
    ):
        monkeypatch.setattr("django_ergo.bots.pools.disabled_providers", set)
        monkeypatch.setattr(
            "django_ergo.bots.pools.paid_dispatch_problem", lambda **kwargs: ""
        )
        monkeypatch.setattr(
            "django_ergo.conversation.spend_guard.paid_dispatch_problem",
            lambda **kwargs: "",
        )


@pytest.fixture(autouse=True)
def _isolated_credential_names(monkeypatch):
    """Each test models a fresh process; temporary config files must not persist."""
    from django_ergo import provider_credentials

    monkeypatch.setattr(provider_credentials, "_NAMES", set())
    monkeypatch.setattr(provider_credentials, "_SOURCES", set())
    monkeypatch.setattr(provider_credentials, "_SOURCE_STAMPS", {})
    monkeypatch.setattr(provider_credentials, "_BROKEN_SOURCES", set())
