# Semantic fields and search

The `legacy` extra (`pip install 'django-ergo[legacy]'`) adds embeddings
stored in PostgreSQL with pgvector. It needs PostgreSQL; `django.contrib.postgres`
goes in `INSTALLED_APPS`.

## SemanticTextField

A text field that embeds its content on save. Pair each one with a
`VectorField` named `<field>_embedding`:

```python
from django.db import models
from pgvector.django import VectorField
from django_ergo.fields import SemanticTextField

class Note(models.Model):
    title = models.CharField(max_length=200)
    content = SemanticTextField()
    content_embedding = VectorField(dimensions=1536, null=True, blank=True, editable=False)
    summary = SemanticTextField()
    summary_embedding = VectorField(dimensions=1536, null=True, blank=True, editable=False)
```

Embeddings are regenerated only when the text changes.

## Searching

From highest to lowest level:

```python
from django_ergo.fields import SemanticTextField, generate_embedding, semantic_search, vector_search

SemanticTextField.search_field(Note, "content", "Django", top_k=5)   # field helper
semantic_search(Note, "content_embedding", "machine learning")      # embeds the query
vector_search(Note, "summary_embedding", generate_embedding("AI"))  # a vector you already have
```

Results are querysets ordered by cosine distance, annotated with
`semantic_distance`. Ergo's own `Article` model adds manager methods:
`semantic_search_content`, `semantic_search_summary`,
`multi_field_semantic_search(query, weights={"content": 0.7, "summary": 0.3})`
and their `vector_search_*` counterparts.

## Embedding providers

```python
DJANGO_ERGO = {
    "EMBEDDING_PROVIDER": "django_ergo.embedding_providers.OpenAIEmbeddingProvider",
    "EMBEDDING_PROVIDER_CONFIG": {"model": "text-embedding-3-small", "timeout": 30},
}
```

The OpenAI provider reads `OPENAI_API_KEY` unless `api_key` is set.
`DeterministicEmbeddingProvider` gives stable fake vectors for tests. Write
your own by subclassing `django_ergo.embedding_providers.EmbeddingProvider`
and implementing `generate_embedding(text)` and `get_dimensions()`.

The newer [knowledge foundation](knowledge-foundation.md) does lexical,
semantic and hybrid retrieval over memory, database and Git corpora, with
providers supplied by the host rather than tied to these fields.
