"""
Tests for OpenAI integrations in django_ergo.fields module.

This module provides two-tier testing:
1. Real OpenAI API tests (when TEST_OPENAI=true) that cost credits and save fixtures
2. Mocked tests using saved fixtures for fast unit testing without API costs
"""

import os
from unittest.mock import Mock
from unittest.mock import patch

import openai
import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase

from django_ergo.fields import SemanticTextField
from django_ergo.fields import generate_embedding

from .openai_test_utils import openai_test_manager
from .openai_test_utils import save_openai_fixture

User = get_user_model()


class TestGenerateEmbedding:
    """Test the generate_embedding function with both real and mocked OpenAI API."""

    @pytest.mark.openai_real
    def test_generate_embedding_real_api(self):
        """Test generate_embedding with real OpenAI API (costs credits)."""
        if not openai_test_manager.should_use_real_api():
            pytest.skip("TEST_OPENAI not set - skipping costly API test")

        test_text = "Django web framework for Python development"

        result = generate_embedding(test_text)

        # Verify embedding properties
        assert isinstance(result, list)
        assert len(result) == 1536  # text-embedding-3-small dimension
        assert all(isinstance(x, float) for x in result)

        # Save fixture
        input_data = {"text": test_text}

        mock_response = Mock()
        mock_response.data = [Mock()]
        mock_response.data[0].embedding = result

        save_openai_fixture(
            "generate_embedding_basic", input_data, mock_response, "embeddings"
        )

        print(
            f"✅ Real API test completed. Generated {len(result)}-dimensional embedding"
        )

    @pytest.mark.openai_mocked
    def test_generate_embedding_mocked(self):
        """Test generate_embedding with mocked OpenAI API using saved fixtures."""
        fixture = openai_test_manager.load_fixture("generate_embedding_basic")
        if not fixture:
            pytest.skip("No fixture found - run with TEST_OPENAI=true first")

        mock_response = openai_test_manager.create_mock_response(fixture)

        with patch("openai.embeddings.create", return_value=mock_response):
            result = generate_embedding(fixture.input_data["text"])

        # Verify result matches fixture
        assert result == fixture.response_data["embedding"]
        assert isinstance(result, list)
        assert len(result) == 1536
        assert all(isinstance(x, float) for x in result)


class TestSemanticTextField(TestCase):
    """Test the SemanticTextField custom Django field."""

    @pytest.mark.openai_real
    def test_semantic_text_field_real_api(self):
        """Test SemanticTextField with real OpenAI API."""
        if not openai_test_manager.should_use_real_api():
            pytest.skip("TEST_OPENAI not set - skipping costly API test")

        field = SemanticTextField()
        assert field.auto_embed is True

        pytest.skip(
            "Full model test requires database setup - covered by integration tests"
        )

    @pytest.mark.openai_mocked
    def test_semantic_text_field_mocked(self):
        """Test SemanticTextField with mocked APIs."""
        pytest.skip(
            "Full model test requires database setup - covered by integration tests"
        )

    def test_semantic_text_field_init_defaults(self):
        """Test SemanticTextField default initialization."""
        field = SemanticTextField()
        assert field.auto_embed is True
        assert field.generate_embedding_func is None

    def test_semantic_text_field_custom_embed_func(self):
        """Test SemanticTextField with custom embedding function."""

        def custom_func(text):
            return [0.0] * 1536

        field = SemanticTextField(generate_embedding=custom_func)
        assert field.generate_embedding_func is custom_func
        assert field.auto_embed is True

    def test_semantic_text_field_auto_embed_disabled(self):
        """Test SemanticTextField with auto_embed disabled."""
        field = SemanticTextField(auto_embed=False)
        assert field.auto_embed is False


class TestOpenAIIntegrationErrors:
    """Test error handling in OpenAI integrations."""

    @pytest.fixture(autouse=True)
    def mocked_provider_settings(self, settings, monkeypatch):
        """Select the adapter under test independently of the host's default."""
        placeholder = "test-placeholder-not-a-credential"
        monkeypatch.setattr(openai, "api_key", placeholder)
        settings.DJANGO_ERGO = {
            **getattr(settings, "DJANGO_ERGO", {}),
            "EMBEDDING_PROVIDER": "django_ergo.embedding_providers.OpenAIEmbeddingProvider",
            "EMBEDDING_PROVIDER_CONFIG": {"api_key": placeholder},
        }

    def test_generate_embedding_api_error(self):
        """Test generate_embedding error handling."""
        with patch("openai.embeddings.create") as mock_create:
            mock_create.side_effect = Exception("API Error")

            with pytest.raises(Exception, match="API Error"):
                generate_embedding("Some text")


# Test configuration and fixtures
@pytest.fixture(scope="session")
def openai_api_key():
    """Ensure OpenAI API key is available for real API tests."""
    if openai_test_manager.should_use_real_api():
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            pytest.skip("OPENAI_API_KEY not set - cannot run real API tests")
        return api_key
    return None


class TestFixtureGeneration:
    """Tests specifically for fixture generation and management."""

    def test_fixture_directory_creation(self):
        """Test that fixture directory is created properly."""
        assert openai_test_manager.fixtures_dir.exists()
        assert openai_test_manager.fixtures_dir.is_dir()

    def test_fixture_loading_nonexistent(self):
        """Test loading a fixture that doesn't exist."""
        fixture = openai_test_manager.load_fixture("nonexistent_test")
        assert fixture is None
