# Pluggable Embedding System

We've successfully implemented a **pluggable embedding system** for Django Ergo! This allows you to switch between different embedding providers based on your needs.

## ✅ What's Implemented

### 1. Abstract Embedding Provider Interface

- `EmbeddingProvider` base class with standard interface
- All providers implement `generate_embedding()` and `get_dimensions()`
- Clean error handling with `EmbeddingError`

### 2. Three Built-in Providers

#### OpenAI Provider (Default)

```python
# Uses OpenAI's text-embedding-3-small model
# Requires OPENAI_API_KEY environment variable or config
```

#### Deterministic Provider

```python
# Generates deterministic embeddings for testing
# Perfect for CI/CD and development environments
```

#### Custom Provider

```python
# Load pre-computed embeddings from fixtures
# Great for testing with known data
```

### 3. Settings-based Configuration

```python
# In your Django settings
DJANGO_ERGO = {
    'EMBEDDING_PROVIDER': 'django_ergo.embedding_providers.OpenAIEmbeddingProvider',
    'EMBEDDING_PROVIDER_CONFIG': {
        'model': 'text-embedding-3-small',
        'api_key': 'your-key-here',  # Optional, uses env var by default
        'timeout': 30,
    }
}
```

## 🚀 Usage Examples

### Switching to Deterministic Provider for Development

```python
DJANGO_ERGO = {
    'EMBEDDING_PROVIDER': 'django_ergo.embedding_providers.DeterministicEmbeddingProvider',
    'EMBEDDING_PROVIDER_CONFIG': {
        'dimensions': 1536,
        'seed': 42  # For reproducible results
    }
}
```

### Using Custom Embeddings for Testing

```python
DJANGO_ERGO = {
    'EMBEDDING_PROVIDER': 'django_ergo.embedding_providers.CustomEmbeddingProvider',
    'EMBEDDING_PROVIDER_CONFIG': {
        'dimensions': 1536,
        'embeddings': {
            'machine learning': [0.8, 0.1, 0.3, ...],
            'deep learning': [0.7, 0.2, 0.4, ...],
        }
    }
}
```

### Direct Usage in Code

```python
from django_ergo.embedding_providers import get_embedding_provider

# Get the configured provider
provider = get_embedding_provider()

# Generate embeddings
embedding = provider.generate_embedding("Hello, world!")
print(f"Generated {len(embedding)}-dimensional embedding")

# Get provider info
print(f"Using: {provider.name}")
print(f"Dimensions: {provider.get_dimensions()}")
```

## 🧪 Backwards Compatibility

The existing `generate_embedding()` function in `fields.py` now uses the pluggable system:

```python
from django_ergo.fields import generate_embedding

# This now uses your configured provider
embedding = generate_embedding("Some text")
```

## 🔄 Seamless Integration

The `SemanticTextField` automatically uses the configured provider:

```python
class Article(models.Model):
    content = SemanticTextField()
    content_embedding = VectorField(dimensions=1536, null=True, blank=True)

# When you save an Article, it automatically uses your configured provider
article = Article(content="This will be embedded automatically")
article.save()  # Uses configured embedding provider
```

## 🎯 Benefits

1. **Environment-specific providers**: Use test providers in CI, OpenAI in production
2. **Cost control**: Avoid OpenAI costs during development and testing
3. **Reproducible tests**: Deterministic embeddings for reliable testing
4. **Flexibility**: Easy to add new providers (Hugging Face, local models, etc.)
5. **Zero breaking changes**: Existing code continues to work

## 🚀 What's Next?

The pluggable embedding system opens the door for:

- Adding Hugging Face Transformers provider
- Local model providers (sentence-transformers)
- Cached embedding providers
- Multi-provider fallback systems
- Custom dimension optimization

Ready to continue with the next TODO item: **Enhanced Search** features!
