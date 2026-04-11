"""
Django Ergo - Pluggable Embedding System

This module provides the embedding provider interface and implementations for different
embedding services. The system supports multiple providers through a pluggable architecture.

## Available Providers

### OpenAIEmbeddingProvider (Default)
Uses OpenAI's text-embedding-3-small model to generate 1536-dimensional vectors.

### TestEmbeddingProvider
For testing and development - returns deterministic vectors based on text hash.

### CustomEmbeddingProvider
For loading pre-computed embeddings from fixtures or custom implementations.

## Configuration

Set your preferred provider in Django settings:

```python
DJANGO_ERGO = {
    'EMBEDDING_PROVIDER': 'django_ergo.embedding_providers.OpenAIEmbeddingProvider',
    'EMBEDDING_PROVIDER_CONFIG': {
        'model': 'text-embedding-3-small',  # OpenAI specific
        'api_key': 'your-key-here',
    }
}
```

## Usage

```python
from django_ergo.embedding_providers import get_embedding_provider

provider = get_embedding_provider()
embedding = provider.generate_embedding("Hello world")
dimensions = provider.get_dimensions()
```
"""

import hashlib
import logging
import os
from abc import ABC
from abc import abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class EmbeddingProvider(ABC):
    """
    Abstract base class for embedding providers.

    All embedding providers must implement this interface to be compatible
    with Django Ergo's semantic field system.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        """
        Initialize the embedding provider with configuration.

        Args:
            config: Provider-specific configuration dictionary
        """
        self.config = config or {}

    @abstractmethod
    def generate_embedding(self, text: str) -> list[float]:
        """
        Generate an embedding vector for the given text.

        Args:
            text: The text to embed

        Returns:
            List[float]: The embedding vector

        Raises:
            EmbeddingError: If embedding generation fails
        """

    @abstractmethod
    def get_dimensions(self) -> int:
        """
        Get the dimensionality of embeddings produced by this provider.

        Returns:
            int: Number of dimensions in the embedding vectors
        """

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Get the name of this embedding provider.

        Returns:
            str: Provider name for identification
        """


class EmbeddingError(Exception):
    """Exception raised when embedding generation fails."""


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """
    OpenAI embedding provider using text-embedding-3-small model.

    Configuration options:
    - model: OpenAI model name (default: text-embedding-3-small)
    - api_key: OpenAI API key (falls back to OPENAI_API_KEY env var)
    - timeout: Request timeout in seconds (default: 30)
    """

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)

        # Import OpenAI here to make it optional for other providers
        try:
            import openai

            self.openai = openai
        except ImportError as err:
            msg = (
                "OpenAI package is required for OpenAIEmbeddingProvider. "
                "Install with: pip install openai"
            )
            raise ImportError(msg) from err

        # Configure OpenAI client
        self.model = self.config.get("model", "text-embedding-3-small")
        self.api_key = self.config.get("api_key") or os.getenv("OPENAI_API_KEY")
        self.timeout = self.config.get("timeout", 30)

        if not self.api_key:
            msg = (
                "OpenAI API key is required. Set OPENAI_API_KEY environment variable "
                "or provide 'api_key' in provider config."
            )
            raise ValueError(msg)

        # Set the API key
        self.openai.api_key = self.api_key

    def generate_embedding(self, text: str) -> list[float]:
        """
        Generate embeddings using OpenAI's embedding API.

        Args:
            text: The text to embed

        Returns:
            List[float]: 1536-dimensional embedding vector

        Raises:
            EmbeddingError: If the OpenAI API call fails
        """
        if not text or not text.strip():
            # Return zero vector for empty text
            return [0.0] * self.get_dimensions()

        try:
            response = self.openai.embeddings.create(
                model=self.model, input=text.strip(), encoding_format="float"
            )

            embedding = response.data[0].embedding

            if len(embedding) != self.get_dimensions():
                msg = (
                    f"Expected {self.get_dimensions()} dimensions, got {len(embedding)}"
                )
                raise EmbeddingError(msg)  # noqa: TRY301

            return embedding

        except Exception as e:
            logger.exception("OpenAI embedding generation failed")
            msg = f"Failed to generate OpenAI embedding: {e}"
            raise EmbeddingError(msg) from e

    def get_dimensions(self) -> int:
        """
        Get dimensions for the current model.

        Returns:
            int: 1536 for text-embedding-3-small
        """
        # Known dimensions for OpenAI models
        model_dimensions = {
            "text-embedding-3-small": 1536,
            "text-embedding-3-large": 3072,
            "text-embedding-ada-002": 1536,
        }

        return model_dimensions.get(self.model, 1536)

    @property
    def name(self) -> str:
        return f"OpenAI ({self.model})"


class DeterministicEmbeddingProvider(EmbeddingProvider):
    """
    Deterministic embedding provider for development and testing.

    Generates deterministic embeddings based on text hash, ensuring
    consistent results across test runs.

    Configuration options:
    - dimensions: Number of dimensions (default: 1536)
    - seed: Random seed for consistency (default: 42)
    """

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)

        self.dimensions = self.config.get("dimensions", 1536)
        self.seed = self.config.get("seed", 42)

    def generate_embedding(self, text: str) -> list[float]:
        """
        Generate deterministic embedding based on text hash.

        Args:
            text: The text to embed

        Returns:
            List[float]: Deterministic embedding vector
        """
        if not text or not text.strip():
            return [0.0] * self.dimensions

        # Create deterministic hash-based embedding
        text_hash = hashlib.md5(
            text.strip().encode(), usedforsecurity=False
        ).hexdigest()

        # Convert hash to numbers and normalize
        embedding = []
        for i in range(self.dimensions):
            # Use different parts of the hash for variety
            hash_segment = text_hash[
                (i * 2) % len(text_hash) : (i * 2) % len(text_hash) + 2
            ]
            value = int(hash_segment, 16) / 255.0  # Normalize to 0-1
            value = (value - 0.5) * 2  # Normalize to -1 to 1
            embedding.append(value)

        return embedding

    def get_dimensions(self) -> int:
        return self.dimensions

    @property
    def name(self) -> str:
        return f"Deterministic Provider ({self.dimensions}D)"


class CustomEmbeddingProvider(EmbeddingProvider):
    """
    Custom embedding provider for loading pre-computed embeddings.

    Useful for testing with fixtures or when you have pre-computed
    embeddings from external sources.

    Configuration options:
    - embeddings: Dict mapping text to embedding vectors
    - default_embedding: Default vector for unknown text
    - dimensions: Number of dimensions
    """

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)

        self.embeddings = self.config.get("embeddings", {})
        self.dimensions = self.config.get("dimensions", 1536)
        self.default_embedding = self.config.get(
            "default_embedding", [0.0] * self.dimensions
        )

    def generate_embedding(self, text: str) -> list[float]:
        """
        Return pre-computed embedding or default.

        Args:
            text: The text to embed

        Returns:
            List[float]: Pre-computed or default embedding vector
        """
        if not text or not text.strip():
            return [0.0] * self.dimensions

        # Return pre-computed embedding if available
        return self.embeddings.get(text.strip(), self.default_embedding)

    def add_embedding(self, text: str, embedding: list[float]) -> None:
        """
        Add a pre-computed embedding for the given text.

        Args:
            text: The text
            embedding: The embedding vector
        """
        self.embeddings[text] = embedding

    def get_dimensions(self) -> int:
        return self.dimensions

    @property
    def name(self) -> str:
        return f"Custom Provider ({len(self.embeddings)} embeddings)"


# Provider registry
EMBEDDING_PROVIDERS = {
    "openai": OpenAIEmbeddingProvider,
    "deterministic": DeterministicEmbeddingProvider,
    "test": DeterministicEmbeddingProvider,  # Alias for backward compatibility
    "custom": CustomEmbeddingProvider,
}


def get_embedding_provider() -> EmbeddingProvider:
    """
    Get the configured embedding provider instance.

    Returns:
        EmbeddingProvider: Configured provider instance

    Raises:
        ImportError: If provider class cannot be imported
        ValueError: If provider is not configured properly
    """
    from django_ergo.settings import api_settings

    # Get provider class path from settings
    provider_path = api_settings.EMBEDDING_PROVIDER
    provider_config = api_settings.EMBEDDING_PROVIDER_CONFIG

    # Import provider class
    if isinstance(provider_path, str):
        # Handle string import path
        from django.utils.module_loading import import_string

        try:
            provider_class = import_string(provider_path)
        except ImportError as e:
            msg = f"Cannot import embedding provider '{provider_path}': {e}"
            raise ImportError(msg) from e
    else:
        # Direct class reference
        provider_class = provider_path

    # Validate provider class
    if not issubclass(provider_class, EmbeddingProvider):
        msg = f"Provider {provider_class} must inherit from EmbeddingProvider"
        raise TypeError(msg)

    # Create provider instance
    try:
        return provider_class(provider_config)
    except Exception as e:  # noqa: BLE001
        msg = f"Failed to initialize embedding provider: {e}"
        raise ValueError(msg) from e
