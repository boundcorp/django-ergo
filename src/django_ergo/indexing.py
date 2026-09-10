"""Explicit embedding/indexing operations for Article projections."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.db import transaction

from django_ergo.embedding_providers import EmbeddingProvider
from django_ergo.embedding_providers import get_embedding_provider
from django_ergo.models import Article


class IndexingError(RuntimeError):
    """Raised when an embedding cannot be stored safely."""


def _embedding_dimensions(article: Article, field_name: str) -> int:
    field = article._meta.get_field(f"{field_name}_embedding")  # noqa: SLF001
    return field.dimensions


def _generate(
    article: Article,
    field_name: str,
    provider: EmbeddingProvider,
) -> list[float] | None:
    text = getattr(article, field_name) or ""
    if not text.strip():
        return None
    embedding = provider.generate_embedding(text)
    dimensions = _embedding_dimensions(article, field_name)
    if len(embedding) != dimensions:
        raise IndexingError(
            f"{provider.name} returned {len(embedding)} dimensions for "
            f"{field_name}; expected {dimensions}."
        )
    return embedding


def index_article(
    article: Article,
    *,
    provider: EmbeddingProvider | None = None,
    fields: Iterable[str] = ("content",),
) -> Article:
    """Generate and persist embeddings for one Article explicitly.

    Managed projections use explicit indexing. Unmanaged legacy Article saves
    retain their automatic embedding behavior.
    """
    provider = provider or get_embedding_provider()
    field_names = tuple(fields)
    unknown_fields = set(field_names) - {"content", "summary"}
    if unknown_fields:
        raise ValueError(f"Unsupported semantic fields: {sorted(unknown_fields)}")

    updates: dict[str, Any] = {
        f"{field_name}_embedding": _generate(article, field_name, provider)
        for field_name in field_names
    }
    if not updates:
        return article

    with transaction.atomic():
        for field_name, value in updates.items():
            setattr(article, field_name, value)
        article.save(
            update_fields=[*updates, "updated_at"],
            _allow_managed_write=True,
        )
    return article


def index_articles(
    articles: Iterable[Article],
    *,
    provider: EmbeddingProvider | None = None,
    fields: Iterable[str] = ("content",),
) -> int:
    """Index a batch with one provider instance and return its count."""
    provider = provider or get_embedding_provider()
    count = 0
    for article in articles:
        index_article(article, provider=provider, fields=fields)
        count += 1
    return count
