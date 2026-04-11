# django-ergo

![Coverage](https://img.shields.io/badge/coverage-97%25-brightgreen)

## Overview

AI Knowledgebase Toolkit for Django

**Django Ergo** provides powerful semantic search and knowledge management capabilities through innovative field types and AI integration. Build intelligent Django applications with automatic embedding generation, multi-field semantic search, and advanced workflow orchestration.

## ✨ Key Features

- **🧠 SemanticTextField**: Revolutionary field type combining text + auto-generated embeddings
- **📊 Multi-Field Vector Search**: Search across multiple semantic fields with custom weighting
- **🔍 Advanced Search Capabilities**: Content-specific, summary-specific, and combined semantic search
- **⚡ Workflow Engine**: Python-based with pause/resume and state persistence
- **🔧 Tool System**: Declarative tool registry with approval workflows
- **🔌 MCP Integration**: Build Model Context Protocol servers with reusable tools
- **👥 Multi-Tenant**: User-scoped knowledge bases and workflows
- **⚙️ Admin Interface**: Full Django admin integration

## 🚀 SemanticTextField - Revolutionary Field Type

The `SemanticTextField` is Django Ergo's innovative field type that automatically creates semantic embeddings for any text content. Combined with our modular search architecture, it provides unparalleled flexibility for AI-powered applications.

```python
from django_ergo.fields import SemanticTextField, semantic_search, vector_search
from pgvector.django import VectorField

class Article(models.Model):
    title = models.CharField(max_length=200)
    content = SemanticTextField(help_text="Main article content")
    content_embedding = VectorField(dimensions=1536, null=True, blank=True, editable=False)
    summary = SemanticTextField(help_text="Article summary")
    summary_embedding = VectorField(dimensions=1536, null=True, blank=True, editable=False)

# Multiple ways to search - choose your level of control:

# 1. High-level semantic search (auto-embeds query)
results = semantic_search(Article, 'content_embedding', 'machine learning')

# 2. Low-level vector search (use pre-computed vectors)
vector = generate_embedding('AI development')
results = vector_search(Article, 'summary_embedding', vector)

# 3. Field helper methods
results = SemanticTextField.search_field(Article, 'content', 'Django')

# 4. QuerySet methods with advanced features
results = Article.objects.semantic_search_content('programming')
results = Article.objects.multi_field_semantic_search(
    'AI development',
    weights={'content': 0.7, 'summary': 0.3}
)
```

### Modular Architecture Benefits

- 🎯 **Multiple embeddings per model** - Each semantic field gets its own embedding
- 🔍 **Field-specific search** - Search content vs summary independently
- ⚖️ **Weighted multi-field search** - Combine fields with custom weights
- 🏗️ **Auto-generated fields** - No manual embedding field management
- 🔄 **Automatic updates** - Embeddings regenerate when content changes
- 📊 **Distance scoring** - Get semantic similarity scores for ranking
- 🛠️ **Modular functions** - Low-level and high-level search APIs
- ⚡ **Performance optimized** - Efficient vector operations with pgvector

## Quickstart

Install django-ergo:

```bash
# From pypi
python3 -m pip install django-ergo

# From source
python3 -m pip install git+https://github.com/boundcorp/django-ergo.git
```

### Settings

To enable `django_ergo` in your project you need to add it to `INSTALLED_APPS` in your projects `settings.py` file:

```python
INSTALLED_APPS = (
    ...
    'django_ergo',
    ...
)
```

Add django-ergo's URL patterns:

```python
from django_ergo import urls as django_ergo_urls


urlpatterns = [
    ...
    path(r"", include(django_ergo_urls, namespace='django-ergo')),
    ...
]
```

## Development

```bash
make env
make pip_install
make migrations
make migrate
make superuser
make serve
```

- Visit `http://127.0.0.1:8000/` for the default "It worked" page
- Visit `http://127.0.0.1:8000/admin/` for the Django Admin

### Testing

```bash
make pytest
make coverage
make open_coverage
```

## Deploying

```bash
# Publish to PyPI Test before the live PyPi
make release_test
make release
```

## Issues

If you experience any issues, please create an [issue](https://github.org/boundcorp/django-ergo/issues) on Github.
