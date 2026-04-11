"""
Django Ergo - Semantic Text Fields and Vector Search

This module provides innovative field types and search functions for semantic AI applications.

## Core Components

### 1. SemanticTextField
Revolutionary Django field that combines text content with automatic embedding generation:
- Automatically creates associated VectorField for embeddings
- Generates embeddings when content changes
- Provides field-specific search methods

### 2. Modular Search Functions
Low-level and high-level search functions for maximum flexibility:
- vector_search(): Low-level search with pre-computed vectors
- semantic_search(): High-level search that embeds query text
- Field helpers: SemanticTextField.search_field() and search_field_vector()

## Usage Examples

```python
from django_ergo.fields import SemanticTextField, semantic_search, vector_search

# Field definition
class Article(models.Model):
    content = SemanticTextField(help_text="Main content")
    summary = SemanticTextField(help_text="Brief summary")
    # Auto-creates: content_embedding, summary_embedding

# High-level semantic search (embeds query automatically)
results = semantic_search(Article, 'content_embedding', 'machine learning')

# Low-level vector search (use pre-computed vector)
vector = generate_embedding('AI development')
results = vector_search(Article, 'summary_embedding', vector)

# Field helper methods
results = SemanticTextField.search_field(Article, 'content', 'Django')
results = SemanticTextField.search_field_vector(Article, 'summary', vector)

# QuerySet methods (available on Article.objects)
results = Article.objects.semantic_search_content('programming')
results = Article.objects.vector_search_summary(vector)
results = Article.objects.multi_field_semantic_search('AI', weights={'content': 0.7})
```

## Architecture Benefits
- 🎯 Multiple embeddings per model
- 🔍 Field-specific semantic search
- ⚖️ Weighted multi-field search
- 🏗️ Auto-generated embedding fields
- 🔄 Automatic embedding updates
- 📊 Distance scoring for ranking
- 🛠️ Modular search functions for flexibility
"""

import logging
from typing import Any

from django.db import models
from pgvector.django import CosineDistance

logger = logging.getLogger(__name__)


def generate_summary(
    text: str, max_tokens: int = 100, user_context: dict[str, Any] | None = None
) -> str:
    """
    Generate a summary of the text using OpenAI's API.

    Args:
        text: The text to summarize
        max_tokens: Maximum tokens for the summary
        user_context: Optional context for the summary

    Returns:
        str: Generated summary
    """
    # Import here to avoid circular imports
    import os

    import openai

    # Configure OpenAI client - this still uses direct OpenAI for now
    # TODO: Consider making summary generation pluggable as well
    openai.api_key = os.getenv("OPENAI_API_KEY", "test-key-for-development")

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": f"Return a summary of the text that is {max_tokens} tokens or less. Optimize for token usage, omit non-essential information.",
        }
    ]

    if user_context:
        messages.append(
            {
                "role": "function",
                "name": "user_context",
                "content": str(user_context),
            }
        )

    messages.append({"role": "user", "content": text})

    response = openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,  # type: ignore[arg-type]
    )

    content = response.choices[0].message.content
    if content is None:
        empty_content_error = "OpenAI returned empty content"
        raise ValueError(empty_content_error)

    return content


def generate_embedding(text: str) -> list[float]:
    """
    Generate embeddings using the configured embedding provider.

    Args:
        text: The text to embed

    Returns:
        List[float]: Embedding vector (dimensions depend on provider)

    Raises:
        EmbeddingError: If embedding generation fails
    """
    from django_ergo.embedding_providers import get_embedding_provider

    provider = get_embedding_provider()
    return provider.generate_embedding(text)


def vector_search(
    model_class, vector_field_name: str, query_vector: list[float], top_k: int = 10
):
    """
    Low-level vector search function that searches for a known vector.

    Args:
        model_class: The Django model class to search
        vector_field_name: Name of the vector field to search against
        query_vector: The embedding vector to search for
        top_k: Number of results to return

    Returns:
        QuerySet: Results ordered by semantic similarity (cosine distance)
    """
    return (
        model_class.objects.exclude(**{f"{vector_field_name}__isnull": True})
        .annotate(semantic_distance=CosineDistance(vector_field_name, query_vector))
        .order_by("semantic_distance")[:top_k]
    )


def semantic_search(
    model_class, vector_field_name: str, query_text: str, top_k: int = 10
):
    """
    High-level semantic search function that embeds the query and searches for that vector.

    Args:
        model_class: The Django model class to search
        vector_field_name: Name of the vector field to search against
        query_text: Text query to embed and search for
        top_k: Number of results to return

    Returns:
        QuerySet: Results ordered by semantic similarity
    """
    # Generate embedding for the query text
    query_vector = generate_embedding(query_text)

    # Use low-level vector search
    return vector_search(model_class, vector_field_name, query_vector, top_k)


class SemanticTextField(models.TextField):
    """
    A custom Django field that automatically generates embeddings for text content
    when the field value changes. Used in combination with manually defined VectorFields.

    This field acts as a TextField but automatically:
    1. Generates embeddings when the text content changes
    2. Updates the associated embedding field
    3. Provides semantic search helper methods

    Usage:
        class Article(models.Model):
            content = SemanticTextField(help_text="Main article content")
            content_embedding = VectorField(dimensions=1536, null=True, blank=True, editable=False)
            summary = SemanticTextField(help_text="Article summary")
            summary_embedding = VectorField(dimensions=1536, null=True, blank=True, editable=False)

        # Search using the field helper methods:
        results = SemanticTextField.search_field(Article, 'content', 'search query')

        # Or use the module-level functions directly:
        results = semantic_search(Article, 'content_embedding', 'search query')
        results = vector_search(Article, 'content_embedding', embedding_vector)
    """

    def __init__(self, *args, **kwargs):
        # Custom options for embedding generation
        self.generate_embedding_func = kwargs.pop("generate_embedding", None)
        self.auto_embed = kwargs.pop("auto_embed", True)

        super().__init__(*args, **kwargs)

    def pre_save(self, model_instance, add):
        """
        Generate embedding if the text content has changed.
        """
        if not self.auto_embed:
            return super().pre_save(model_instance, add)

        # Get current text value
        current_text = getattr(model_instance, self.attname)

        # Skip processing if text is empty
        if not current_text or not current_text.strip():
            return current_text

        # Determine if we need to regenerate embedding
        has_changed = self._has_field_changed(model_instance, current_text)

        if has_changed:
            try:
                # Generate embedding vector using custom function or provider system
                if self.generate_embedding_func:
                    embedding = self.generate_embedding_func(current_text)
                else:
                    # Use the pluggable provider system
                    from django_ergo.embedding_providers import get_embedding_provider

                    provider = get_embedding_provider()
                    embedding = provider.generate_embedding(current_text)

                # Set embedding field on model instance (expecting {field_name}_embedding)
                embedding_field_name = f"{self.attname}_embedding"
                if hasattr(model_instance, embedding_field_name):
                    setattr(model_instance, embedding_field_name, embedding)
                else:
                    logger.warning(
                        "No embedding field '%s' found for SemanticTextField '%s'",
                        embedding_field_name,
                        self.attname,
                    )

            except Exception:
                logger.exception(
                    "Error processing SemanticTextField '%s'", self.attname
                )
                # Set null embedding on error to avoid breaking saves
                embedding_field_name = f"{self.attname}_embedding"
                if hasattr(model_instance, embedding_field_name):
                    setattr(model_instance, embedding_field_name, None)

        return current_text

    def _has_field_changed(self, instance, current_text):
        """
        Check if the field value has changed since the last save.
        """
        if instance._state.adding:  # noqa: SLF001
            return True  # New instance always triggers generation
        # Check database for previous value
        try:
            old_value = (
                instance.__class__.objects.filter(pk=instance.pk)
                .values_list(self.attname, flat=True)
                .first()
            )
            return old_value != current_text
        except Exception:  # noqa: BLE001
            # If we can't determine the old value, assume it changed
            return True

    @classmethod
    def search_field(
        cls, model_class, field_name: str, query_text: str, top_k: int = 10
    ):
        """
        High-level semantic search on a specific SemanticTextField.

        Args:
            model_class: The Django model class
            field_name: Name of the SemanticTextField to search
            query_text: Search query text
            top_k: Number of results to return

        Returns:
            QuerySet: Results ordered by semantic similarity
        """
        # Get the embedding field name (follows pattern: {field_name}_embedding)
        embedding_field_name = f"{field_name}_embedding"

        # Use high-level semantic search
        return semantic_search(model_class, embedding_field_name, query_text, top_k)

    @classmethod
    def search_field_vector(
        cls, model_class, field_name: str, query_vector: list[float], top_k: int = 10
    ):
        """
        Low-level vector search on a specific SemanticTextField.

        Args:
            model_class: The Django model class
            field_name: Name of the SemanticTextField to search
            query_vector: Pre-computed embedding vector
            top_k: Number of results to return

        Returns:
            QuerySet: Results ordered by semantic similarity
        """
        # Get the embedding field name (follows pattern: {field_name}_embedding)
        embedding_field_name = f"{field_name}_embedding"

        # Use low-level vector search
        return vector_search(model_class, embedding_field_name, query_vector, top_k)
