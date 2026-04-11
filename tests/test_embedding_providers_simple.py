"""
Simple tests for the pluggable embedding system that don't require Django setup.

These tests verify that the embedding provider system works correctly
without needing full Django configuration.
"""

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from django_ergo.embedding_providers import CustomEmbeddingProvider

# Import the classes directly
from django_ergo.embedding_providers import DeterministicEmbeddingProvider
from django_ergo.embedding_providers import EmbeddingError
from django_ergo.embedding_providers import OpenAIEmbeddingProvider


class TestDeterministicEmbeddingProvider:
    """Test the deterministic embedding provider."""

    def test_basic_functionality(self):
        """Test basic functionality of DeterministicEmbeddingProvider."""
        provider = DeterministicEmbeddingProvider()

        # Test basic properties
        assert provider.get_dimensions() == 1536
        assert provider.name.startswith("Deterministic Provider")

        # Test embedding generation
        embedding1 = provider.generate_embedding("Hello world")
        embedding2 = provider.generate_embedding("Hello world")

        # Should be deterministic
        assert embedding1 == embedding2
        assert len(embedding1) == 1536

        # Different text should produce different embeddings
        embedding3 = provider.generate_embedding("Different text")
        assert embedding1 != embedding3

    def test_custom_dimensions(self):
        """Test DeterministicEmbeddingProvider with custom dimensions."""
        provider = DeterministicEmbeddingProvider(config={"dimensions": 512})

        assert provider.get_dimensions() == 512

        embedding = provider.generate_embedding("Test text")
        assert len(embedding) == 512

    def test_empty_text(self):
        """Test DeterministicEmbeddingProvider with empty text."""
        provider = DeterministicEmbeddingProvider()

        embedding = provider.generate_embedding("")
        assert embedding == [0.0] * 1536

        embedding = provider.generate_embedding("   ")
        assert embedding == [0.0] * 1536


class TestCustomEmbeddingProvider:
    """Test the custom embedding provider."""

    def test_basic_functionality(self):
        """Test basic functionality of CustomEmbeddingProvider."""
        embeddings = {
            "hello": [0.1, 0.2, 0.3],
            "world": [0.4, 0.5, 0.6],
        }

        provider = CustomEmbeddingProvider(
            config={
                "embeddings": embeddings,
                "dimensions": 3,
            }
        )

        assert provider.get_dimensions() == 3
        assert "2 embeddings" in provider.name

        # Test known embeddings
        assert provider.generate_embedding("hello") == [0.1, 0.2, 0.3]
        assert provider.generate_embedding("world") == [0.4, 0.5, 0.6]

        # Test unknown text (should return default)
        default_embedding = provider.generate_embedding("unknown")
        assert default_embedding == [0.0, 0.0, 0.0]

    def test_add_embedding(self):
        """Test adding embeddings to CustomEmbeddingProvider."""
        provider = CustomEmbeddingProvider(config={"dimensions": 2})

        provider.add_embedding("test", [0.7, 0.8])

        embedding = provider.generate_embedding("test")
        assert embedding == [0.7, 0.8]


class TestOpenAIEmbeddingProvider:
    """Test the OpenAI embedding provider."""

    def test_missing_api_key(self):
        """Test OpenAI provider fails without API key."""
        with (
            patch.dict("os.environ", {}, clear=True),
            pytest.raises(ValueError, match="OpenAI API key is required"),
        ):
            OpenAIEmbeddingProvider()

    def test_with_config_key(self):
        """Test OpenAI provider with API key in config."""
        config = {"api_key": "test-key"}

        # Mock the import to avoid actual OpenAI dependency
        mock_openai = MagicMock()
        with patch.dict("sys.modules", {"openai": mock_openai}):
            provider = OpenAIEmbeddingProvider(config)

            assert provider.api_key == "test-key"
            assert provider.model == "text-embedding-3-small"
            assert provider.get_dimensions() == 1536

    def test_embedding_generation(self):
        """Test OpenAI embedding generation."""
        # Mock OpenAI response
        mock_openai = MagicMock()
        mock_response = MagicMock()
        mock_response.data = [MagicMock()]
        mock_response.data[0].embedding = [0.1] * 1536
        mock_openai.embeddings.create.return_value = mock_response

        with patch.dict("sys.modules", {"openai": mock_openai}):
            config = {"api_key": "test-key"}
            provider = OpenAIEmbeddingProvider(config)

            embedding = provider.generate_embedding("Test text")

            assert len(embedding) == 1536
            assert embedding == [0.1] * 1536

            # Verify OpenAI was called correctly
            provider.openai.embeddings.create.assert_called_once_with(
                model="text-embedding-3-small",
                input="Test text",
                encoding_format="float",
            )

    def test_embedding_error(self):
        """Test OpenAI provider handles API errors."""
        # Mock OpenAI error
        mock_openai = MagicMock()
        mock_openai.embeddings.create.side_effect = Exception("API Error")

        with patch.dict("sys.modules", {"openai": mock_openai}):
            config = {"api_key": "test-key"}
            provider = OpenAIEmbeddingProvider(config)

            with pytest.raises(EmbeddingError) as exc_info:
                provider.generate_embedding("Test text")

            assert "Failed to generate OpenAI embedding" in str(exc_info.value)


def test_embedding_providers_work_without_django():
    """Test that embedding providers work independently of Django configuration."""
    # Test provider works without any Django configuration
    provider = DeterministicEmbeddingProvider()
    embedding = provider.generate_embedding("Hello, world!")

    assert len(embedding) == 1536
    assert isinstance(embedding, list)
    assert all(isinstance(x, int | float) for x in embedding)


def test_custom_provider_with_fixtures():
    """Test custom provider can be used with test fixtures."""
    # Simulate loading test data
    test_embeddings = {
        "machine learning": [0.8, 0.1, 0.3],
        "artificial intelligence": [0.7, 0.2, 0.4],
        "neural networks": [0.6, 0.3, 0.5],
    }

    provider = CustomEmbeddingProvider(
        config={"embeddings": test_embeddings, "dimensions": 3}
    )

    # Test that fixtures work
    assert provider.generate_embedding("machine learning") == [0.8, 0.1, 0.3]
    assert provider.generate_embedding("unknown term") == [0.0, 0.0, 0.0]  # default

    # Test adding new embedding
    provider.add_embedding("deep learning", [0.9, 0.0, 0.2])
    assert provider.generate_embedding("deep learning") == [0.9, 0.0, 0.2]


if __name__ == "__main__":
    # Run tests manually without pytest if needed
    test_funcs = [
        test_embedding_providers_work_without_django,
        test_custom_provider_with_fixtures,
    ]

    for test_func in test_funcs:
        try:
            test_func()
            print(f"✓ {test_func.__name__} passed")
        except Exception as e:  # noqa: BLE001
            print(f"✗ {test_func.__name__} failed: {e}")

    # Test classes
    for test_class in [TestDeterministicEmbeddingProvider, TestCustomEmbeddingProvider]:
        instance = test_class()
        for method_name in dir(instance):
            if method_name.startswith("test_"):
                try:
                    method = getattr(instance, method_name)
                    method()
                    print(f"✓ {test_class.__name__}.{method_name} passed")
                except Exception as e:  # noqa: BLE001
                    print(f"✗ {test_class.__name__}.{method_name} failed: {e}")
