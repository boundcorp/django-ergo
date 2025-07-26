"""
Utilities for testing OpenAI integrations with fixture-based mocking.

Provides two-tier testing:
1. Real OpenAI API tests (when TEST_OPENAI=true) that generate fixtures
2. Mocked tests using saved fixtures for fast unit testing
"""

import json
import os
import pytest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import Mock, patch
from dataclasses import dataclass, asdict


@dataclass
class OpenAIFixture:
    """Container for OpenAI API response data."""
    input_data: Dict[str, Any]
    response_data: Dict[str, Any]
    api_endpoint: str
    timestamp: str


class OpenAITestManager:
    """Manages OpenAI API testing with fixture generation and mocking."""
    
    def __init__(self, fixtures_dir: Optional[Path] = None):
        self.fixtures_dir = fixtures_dir or Path(__file__).parent / "fixtures" / "openai"
        self.fixtures_dir.mkdir(parents=True, exist_ok=True)
        self.test_openai = os.environ.get("TEST_OPENAI", "false").lower() == "true"
        
    def should_use_real_api(self) -> bool:
        """Check if tests should use real OpenAI API."""
        return self.test_openai
    
    def get_fixture_path(self, test_name: str) -> Path:
        """Get the fixture file path for a test."""
        return self.fixtures_dir / f"{test_name}.json"
    
    def save_fixture(self, test_name: str, input_data: Dict, response_data: Dict, api_endpoint: str):
        """Save OpenAI API response as a fixture."""
        from datetime import datetime
        
        fixture = OpenAIFixture(
            input_data=input_data,
            response_data=response_data,
            api_endpoint=api_endpoint,
            timestamp=datetime.now().isoformat()
        )
        
        fixture_path = self.get_fixture_path(test_name)
        with open(fixture_path, 'w') as f:
            json.dump(asdict(fixture), f, indent=2)
        
        print(f"💾 Saved fixture: {fixture_path}")
    
    def load_fixture(self, test_name: str) -> Optional[OpenAIFixture]:
        """Load OpenAI fixture data."""
        fixture_path = self.get_fixture_path(test_name)
        if not fixture_path.exists():
            return None
        
        with open(fixture_path, 'r') as f:
            data = json.load(f)
        
        return OpenAIFixture(**data)
    
    def create_mock_response(self, fixture: OpenAIFixture) -> Mock:
        """Create a mock OpenAI response from fixture data."""
        if fixture.api_endpoint == "chat.completions":
            return self._create_chat_completion_mock(fixture.response_data)
        elif fixture.api_endpoint == "embeddings":
            return self._create_embedding_mock(fixture.response_data)
        else:
            raise ValueError(f"Unknown API endpoint: {fixture.api_endpoint}")
    
    def _create_chat_completion_mock(self, response_data: Dict) -> Mock:
        """Create mock for chat.completions.create response."""
        mock_response = Mock()
        mock_response.choices = [Mock()]
        mock_response.choices[0].message = Mock()
        mock_response.choices[0].message.content = response_data["content"]
        mock_response.usage = Mock()
        mock_response.usage.model_dump.return_value = response_data.get("usage", {})
        return mock_response
    
    def _create_embedding_mock(self, response_data: Dict) -> Mock:
        """Create mock for embeddings.create response."""
        mock_response = Mock()
        mock_response.data = [Mock()]
        mock_response.data[0].embedding = response_data["embedding"]
        return mock_response


# Global test manager instance
openai_test_manager = OpenAITestManager()


def openai_integration_test(test_name: str, api_endpoint: str):
    """
    Decorator for OpenAI integration tests.
    
    Creates two test variants:
    - One that uses real API when TEST_OPENAI=true
    - One that uses fixtures for fast unit testing
    """
    def decorator(test_func):
        def real_api_test(*args, **kwargs):
            """Test that uses real OpenAI API and saves fixtures."""
            if not openai_test_manager.should_use_real_api():
                pytest.skip("TEST_OPENAI not set - skipping costly API test")
            
            # Run the test and capture the result for fixture generation
            return test_func(*args, **kwargs, _save_fixture=True, _test_name=test_name, _api_endpoint=api_endpoint)
        
        def mocked_api_test(*args, **kwargs):
            """Test that uses saved fixtures to mock OpenAI API."""
            fixture = openai_test_manager.load_fixture(test_name)
            if not fixture:
                pytest.skip(f"No fixture found for {test_name} - run with TEST_OPENAI=true first")
            
            # Mock the appropriate OpenAI API call
            mock_response = openai_test_manager.create_mock_response(fixture)
            
            if api_endpoint == "chat.completions":
                with patch('openai.chat.completions.create', return_value=mock_response):
                    return test_func(*args, **kwargs, _fixture=fixture)
            elif api_endpoint == "embeddings":
                with patch('openai.embeddings.create', return_value=mock_response):
                    return test_func(*args, **kwargs, _fixture=fixture)
        
        # Set test names for pytest discovery
        real_api_test.__name__ = f"test_{test_name}_real_api"
        mocked_api_test.__name__ = f"test_{test_name}_mocked"
        
        return real_api_test, mocked_api_test
    
    return decorator


def save_openai_fixture(test_name: str, input_data: Dict, response: Any, api_endpoint: str):
    """Helper to save OpenAI response as fixture during real API tests."""
    if api_endpoint == "chat.completions":
        response_data = {
            "content": response.choices[0].message.content,
            "usage": response.usage.model_dump() if response.usage else {}
        }
    elif api_endpoint == "embeddings":
        response_data = {
            "embedding": response.data[0].embedding
        }
    else:
        response_data = {"raw_response": str(response)}
    
    openai_test_manager.save_fixture(test_name, input_data, response_data, api_endpoint)


# Pytest markers for organizing tests
pytestmark = [
    pytest.mark.openai,  # Mark for all OpenAI-related tests
]


def pytest_configure(config):
    """Configure pytest markers."""
    config.addinivalue_line("markers", "openai: mark test as OpenAI integration test")
    config.addinivalue_line("markers", "openai_real: mark test as requiring real OpenAI API")
    config.addinivalue_line("markers", "openai_mocked: mark test as using mocked OpenAI API")