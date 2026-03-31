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
