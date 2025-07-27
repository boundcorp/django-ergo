# Django Ergo API Example

## Overview

This is an **example API application** built with the Django Ergo plugin to demonstrate how to create RESTful endpoints for AI-powered workflows, knowledge bases, articles, and chat conversations. This API showcases the capabilities of the django-ergo plugin using Django Ninja for fast, modern API development.

> **Note**: This API is **not part of the core django-ergo plugin**. It's an example application located in `tests/example_app/` that demonstrates how to build applications on top of the django-ergo toolkit.

## What is django-ergo?

Django Ergo is a Django plugin that provides:
- **AI Workflow Engine**: Orchestrate AI-powered conversations and tool execution
- **Knowledge Management**: Store and search documents with semantic embeddings  
- **Semantic Search**: AI-powered search using OpenAI embeddings
- **Chat Management**: User conversations with AI agents
- **Tool Registry**: Extensible tools for AI agents

This example API shows how to expose these capabilities through REST endpoints.

## Base URL

```
http://localhost:8000/api/
```

## Quick Start

To run this example:

1. **Install with dev dependencies:**
   ```bash
   pip install -e ".[dev]"
   ```

2. **Set up the database:**
   ```bash
   cd tests/example_app/
   python manage.py migrate
   python manage.py create_sample_data
   ```

3. **Run the server:**
   ```bash
   python manage.py runserver
   ```

4. **Access the API docs:**
   - Interactive Docs: `http://localhost:8000/api/docs/`
   - OpenAPI Schema: `http://localhost:8000/api/openapi.json`

## Interactive Documentation

Django Ninja provides automatic interactive documentation:

- **Swagger UI**: `http://localhost:8000/api/docs/`
- **OpenAPI Schema**: `http://localhost:8000/api/openapi.json`

## Authentication

### JWT Token Authentication

The API uses JWT (JSON Web Token) authentication with a simplified approach.

#### Get Access Token
```http
POST /api/auth/token/
Content-Type: application/json

{
    "username": "your_username", 
    "password": "your_password"
}
```

**Response:**
```json
{
    "access_token": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
    "token_type": "bearer",
    "expires_in": 3600
}
```

#### Using Tokens
Include the access token in the Authorization header for all API requests:

```http
Authorization: Bearer eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...
```

## API Endpoints

### Authentication

#### POST `/auth/token/`
Obtain JWT access token

**Request Body:**
```json
{
    "username": "string",
    "password": "string"
}
```

### Users

#### GET `/users/me/`
Get current user information

**Response:**
```json
{
    "id": 1,
    "username": "john_doe",
    "email": "john@example.com",
    "first_name": "John",
    "last_name": "Doe",
    "date_joined": "2024-01-01T00:00:00Z"
}
```

### Workflows

#### GET `/workflows/`
List all workflows with optional filtering

**Query Parameters:**
- `is_active`: Filter by active status (`true`/`false`)
- `page`: Page number for pagination
- `page_size`: Number of results per page (max 100)

#### GET `/workflows/{workflow_id}/`
Get a specific workflow

#### POST `/workflows/`
Create a new workflow

**Request Body:**
```json
{
    "name": "Customer Support Agent",
    "description": "AI agent for handling customer support inquiries",
    "instructions": "You are a helpful customer support agent. Be polite and provide accurate information.",
    "tools_config": {
        "search_enabled": true,
        "max_search_results": 10
    },
    "is_active": true,
    "knowledgebases": ["kb-uuid-1", "kb-uuid-2"]
}
```

#### PUT `/workflows/{workflow_id}/`
Update a workflow (full update)

#### DELETE `/workflows/{workflow_id}/`
Delete a workflow

### Knowledge Bases

#### GET `/knowledgebases/`
List knowledge bases (user can see their own or public ones)

#### GET `/knowledgebases/{kb_id}/`
Get a specific knowledge base

#### POST `/knowledgebases/`
Create a new knowledge base

**Request Body:**
```json
{
    "name": "Product Documentation",
    "description": "Complete product documentation and guides",
    "workflows": ["workflow-uuid-1", "workflow-uuid-2"]
}
```

#### PUT `/knowledgebases/{kb_id}/`
Update a knowledge base

#### DELETE `/knowledgebases/{kb_id}/`
Delete a knowledge base

#### GET `/knowledgebases/{kb_id}/table_of_contents/`
Get table of contents for a knowledge base

**Response:**
```json
{
    "table_of_contents": "# 0 Getting Started\n# 1 User Guide\n# 2 API Reference"
}
```

### Articles

#### GET `/articles/`
List articles with optional filtering

**Query Parameters:**
- `knowledgebase`: Filter by knowledge base UUID
- `hierarchy_prefix`: Filter by hierarchy code prefix (e.g., "1", "1A")
- `page`: Page number for pagination
- `page_size`: Results per page

#### GET `/articles/{article_id}/`
Get a specific article

#### POST `/articles/`
Create a new article

**Request Body:**
```json
{
    "knowledgebase": "kb-uuid-here",
    "hierarchy_code": "1A2",
    "title": "Getting Started with API",
    "content": "This guide explains how to use the Django Ergo API...",
    "summary": "API getting started guide covering authentication and basic usage"
}
```

#### PUT `/articles/{article_id}/`
Update an article

#### DELETE `/articles/{article_id}/`
Delete an article

#### POST `/articles/search/`
Perform semantic search on articles using AI embeddings

**Request Body:**
```json
{
    "query": "How do I authenticate with the API?",
    "top_k": 10,
    "search_type": "multi_field"
}
```

**Search Types:**
- `content`: Search article content only
- `summary`: Search article summaries only
- `multi_field`: Search both content and summaries (recommended)

**Response:**
```json
{
    "query": "How do I authenticate with the API?",
    "search_type": "multi_field",
    "count": 3,
    "results": [
        {
            "id": "article-uuid",
            "title": "API Authentication",
            "content": "To authenticate with the API...",
            "summary": "Guide to API authentication methods",
            "hierarchy_code": "2A1",
            "knowledgebase": "kb-uuid",
            "knowledgebase_name": "API Documentation",
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z"
        }
    ]
}
```

### User Chats

#### GET `/chats/`
List user's chats (users can only see their own chats)

#### GET `/chats/{chat_id}/`
Get a specific chat

#### POST `/chats/`
Create a new chat

**Request Body:**
```json
{
    "workflow": "workflow-uuid",
    "title": "Support Question",
    "metadata": {
        "priority": "high",
        "category": "billing"
    }
}
```

#### PUT `/chats/{chat_id}/`
Update a chat

#### DELETE `/chats/{chat_id}/`
Delete a chat

#### GET `/chats/{chat_id}/messages/`
Get messages for a chat

#### POST `/chats/{chat_id}/add_message/`
Add a message to a chat

**Request Body:**
```json
{
    "message_type": "user_input",
    "role": "user",
    "content": "How do I cancel my subscription?",
    "metadata": {
        "timestamp": "2024-01-15T10:30:00Z"
    }
}
```

**Message Types:**
- `user_input`: User messages
- `assistant_message`: AI agent responses
- `tool_request`: Tool execution requests
- `tool_response`: Tool execution results
- `system_message`: System notifications
- `error`: Error messages

**Message Roles:**
- `user`: Human user
- `assistant`: AI agent
- `system`: System
- `tool`: Tool execution

### Chat Messages (Direct)

#### GET `/messages/`
List user's chat messages (alternative to chat-based endpoints)

#### GET `/messages/{message_id}/`
Get a specific message

#### POST `/messages/`
Create a new message

#### PUT `/messages/{message_id}/`
Update a message

#### DELETE `/messages/{message_id}/`
Delete a message

## Data Models

### Workflow
```json
{
    "id": "uuid",
    "name": "string",
    "description": "string",
    "instructions": "string",
    "tools_config": {},
    "is_active": true,
    "created_at": "datetime",
    "updated_at": "datetime",
    "knowledgebases_list": ["string"]
}
```

### Knowledge Base
```json
{
    "id": "uuid",
    "name": "string",
    "description": "string",
    "owner_id": "string|null",
    "created_at": "datetime",
    "updated_at": "datetime",
    "article_count": 0
}
```

### Article
```json
{
    "id": "uuid",
    "knowledgebase": "uuid",
    "knowledgebase_name": "string",
    "hierarchy_code": "string",
    "title": "string",
    "content": "string",
    "summary": "string|null",
    "created_at": "datetime",
    "updated_at": "datetime"
}
```

### User Chat
```json
{
    "id": "uuid",
    "user": "integer",
    "user_username": "string",
    "workflow": "uuid",
    "workflow_name": "string",
    "title": "string",
    "is_active": true,
    "metadata": {},
    "created_at": "datetime",
    "updated_at": "datetime",
    "message_count": 0,
    "last_message_at": "datetime|null"
}
```

### Chat Message
```json
{
    "id": "uuid",
    "chat": "uuid",
    "chat_title": "string",
    "message_type": "string",
    "role": "string",
    "content": "string",
    "metadata": {},
    "created_at": "datetime",
    "updated_at": "datetime"
}
```

## Error Handling

The API uses standard HTTP status codes:

- `200 OK`: Success
- `201 Created`: Resource created successfully
- `400 Bad Request`: Invalid request data
- `401 Unauthorized`: Authentication required
- `403 Forbidden`: Permission denied
- `404 Not Found`: Resource not found
- `422 Unprocessable Entity`: Validation error
- `500 Internal Server Error`: Server error

**Error Response Format:**
```json
{
    "detail": "Authentication credentials were not provided.",
    "code": "authentication_failed"
}
```

**Validation Error Format:**
```json
{
    "detail": [
        {
            "loc": ["field_name"],
            "msg": "This field is required.",
            "type": "missing"
        }
    ]
}
```

## Pagination

List endpoints support pagination with the following parameters:

- `page`: Page number (default: 1)
- `page_size`: Results per page (default: 20, max: 100)

**Paginated Response Format:**
```json
{
    "count": 150,
    "next": "http://localhost:8000/api/articles/?page=3",
    "previous": "http://localhost:8000/api/articles/?page=1",
    "results": [...]
}
```

## Example Usage

### Complete Workflow Example

1. **Authenticate:**
```bash
curl -X POST http://localhost:8000/api/auth/token/ \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "admin123"}'
```

2. **Create a Knowledge Base:**
```bash
curl -X POST http://localhost:8000/api/knowledgebases/ \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "FAQ", "description": "Frequently asked questions"}'
```

3. **Add Articles:**
```bash
curl -X POST http://localhost:8000/api/articles/ \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"knowledgebase": "KB_UUID", "hierarchy_code": "1", "title": "How to get started", "content": "Getting started is easy..."}'
```

4. **Search Articles:**
```bash
curl -X POST http://localhost:8000/api/articles/search/ \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "getting started", "top_k": 5}'
```

5. **Create and Use Chat:**
```bash
# Create workflow first, then:
curl -X POST http://localhost:8000/api/chats/ \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"workflow": "WORKFLOW_UUID", "title": "Support Chat"}'

# Add message to chat:
curl -X POST http://localhost:8000/api/chats/CHAT_UUID/add_message/ \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message_type": "user_input", "role": "user", "content": "I need help"}'
```

## Architecture

### Plugin vs Example

- **django-ergo plugin** (`src/django_ergo/`): Core models, tools, workflow engine, fields
- **Example API** (`tests/example_app/`): Demonstrates how to build APIs with the plugin

### Key Example Files

- `api.py`: Django Ninja API endpoints
- `schemas.py`: Pydantic schemas for request/response validation  
- `auth.py`: JWT authentication utilities
- `settings.py`: Configuration for the example app

## Features

### Django Ninja Benefits

- **Fast**: Built on modern Python type hints for better performance
- **Automatic Documentation**: OpenAPI/Swagger docs generated automatically
- **Type Safety**: Full Pydantic integration for request/response validation
- **Modern**: Async support and modern Python features
- **Simple**: Clean, intuitive API design

### Key Features

- **JWT Authentication**: Simple, stateless authentication
- **Semantic Search**: AI-powered article search with OpenAI embeddings
- **User Isolation**: Users can only access their own data
- **Pagination**: Automatic pagination for large datasets
- **Type Validation**: Full request/response validation with Pydantic
- **Auto Documentation**: Interactive API docs with examples

## Building Your Own App

This example shows you how to:

1. **Install django-ergo**: `pip install django-ergo`
2. **Use the models**: Import and use Workflow, Knowledgebase, Article, etc.
3. **Add your own endpoints**: Create custom API endpoints for your use case
4. **Extend functionality**: Add your own tools and workflow logic

```python
# Your Django app
from django_ergo.models import Workflow, Article
from django_ergo.tools import register_tool

# Use the plugin models
workflow = Workflow.objects.create(name="My Workflow")
articles = Article.objects.semantic_search_content("query")

# Add your own tools  
@register_tool("my_custom_tool")
def my_tool(query: str) -> str:
    return f"Processed: {query}"
```

## Rate Limiting

Currently, no rate limiting is implemented, but it's recommended for production deployments.

## Versioning

This example API demonstrates version 1.0.0 of the Django Ergo plugin capabilities.