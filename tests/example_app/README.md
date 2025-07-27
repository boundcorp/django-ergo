# Django Ergo Example API

This is an **example application** that demonstrates how to build a REST API using the `django-ergo` plugin. It's not part of the core plugin itself, but rather showcases how to use django-ergo's models, tools, and workflow engine in a real application.

## What This Example Shows

- ✅ **REST API Development** with Django Ninja
- ✅ **JWT Authentication** for secure access
- ✅ **Semantic Search** using OpenAI embeddings
- ✅ **Knowledge Management** with articles and knowledge bases
- ✅ **AI Workflow Orchestration** with chat management
- ✅ **Automatic API Documentation** with OpenAPI/Swagger

## Quick Start

1. **Install dependencies:**
   ```bash
   cd /path/to/django-ergo
   pip install -e ".[dev]"
   ```

2. **Run migrations:**
   ```bash
   cd tests/example_app/
   python manage.py migrate
   ```

3. **Create sample data:**
   ```bash
   python manage.py create_sample_data
   ```

4. **Start the server:**
   ```bash
   python manage.py runserver
   ```

5. **Explore the API:**
   - Interactive docs: http://localhost:8000/api/docs/
   - Admin interface: http://localhost:8000/admin/ (admin/admin123)

## Key Files

- **`api.py`**: Django Ninja API endpoints
- **`schemas.py`**: Pydantic models for request/response validation
- **`auth.py`**: JWT authentication utilities
- **`settings.py`**: Django configuration with API dependencies

## Architecture

```
django-ergo/
├── src/django_ergo/          # Core plugin
│   ├── models.py            # Database models
│   ├── tools.py             # Tool registry
│   ├── workflow_engine.py   # Workflow orchestration
│   └── fields.py            # Semantic fields
└── tests/example_app/        # This example
    ├── api.py               # REST endpoints
    ├── schemas.py           # API schemas
    ├── auth.py              # Authentication
    └── settings.py          # Configuration
```

## Using in Your Own Project

To build a similar API in your own Django project:

1. **Install django-ergo:**
   ```bash
   pip install django-ergo
   ```

2. **Add to INSTALLED_APPS:**
   ```python
   INSTALLED_APPS = [
       # ... your apps
       'django_ergo',
   ]
   ```

3. **Use the models:**
   ```python
   from django_ergo.models import Workflow, Article

   # Create AI workflows
   workflow = Workflow.objects.create(
       name="Customer Support",
       instructions="You are a helpful assistant..."
   )

   # Search articles semantically
   results = Article.objects.semantic_search_content("user question")
   ```

4. **Add your own API endpoints** (optional):
   - Copy the patterns from this example
   - Use Django Ninja, DRF, or any other framework
   - Customize authentication and permissions

## Features Demonstrated

### 🔐 Authentication
- JWT token-based authentication
- User isolation and permissions
- Secure API access

### 🤖 AI Workflows
- Create and manage AI agent configurations
- Tools and instruction management
- Workflow execution tracking

### 📚 Knowledge Management
- Hierarchical knowledge bases
- Article storage with semantic embeddings
- Multi-field semantic search

### 💬 Chat Management
- User conversations with AI agents
- Message history and metadata
- Workflow-driven responses

### 🔍 Semantic Search
- OpenAI embedding generation
- Vector similarity search
- Hybrid content + summary search

## API Documentation

Full API documentation is available at:
- **Interactive Docs**: http://localhost:8000/api/docs/
- **Written Guide**: [../../docs/api_documentation.md](../../docs/api_documentation.md)

## Dependencies

This example requires additional packages for API functionality:
- `django-ninja` - Modern API framework
- `pydantic` - Data validation
- `python-jose` - JWT handling
- `django-cors-headers` - CORS support

These are included in the `[dev]` optional dependencies of the main package.

## Production Considerations

This is an example for demonstration purposes. For production use:

- ✅ Add rate limiting
- ✅ Implement proper logging
- ✅ Add monitoring and health checks
- ✅ Configure CORS properly
- ✅ Use environment variables for secrets
- ✅ Add comprehensive tests
- ✅ Set up proper database configuration
- ✅ Configure static file serving

## License

Same as the django-ergo plugin - see LICENSE file in the repository root.
