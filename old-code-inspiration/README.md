# django-ergo

## Learn. Think. Become.

Ergo is a Django library for building AI-powered applications with LLM agents, workflows, and knowledgebases. It provides ORM models and utilities for creating intelligent chat systems, document ingestion, and semantic search capabilities.

## Features

### 🤖 User Chat System

- **UserChat**: Individual chat sessions owned by users
- **ChatMessage**: Typed messages (user input, assistant response, tool calls, etc.)
- **Workflow**: AI logic and tools for processing chat messages
- **Message Types**: Support for user_input, assistant_message, tool_request, tool_response, system_message, and error types

### 📚 Knowledgebase System

- **Knowledgebase**: Hierarchical collections of articles
- **Article**: Documents with titles, content, and semantic embeddings
- **Hybrid Search**: Combines PostgreSQL full-text and vector similarity search
- **Hierarchical Organization**: Articles organized with hexadecimal hierarchy codes

### 🔧 Workflow Engine

- **Self-contained**: No external agent library dependencies
- **Tool System**: Extensible tool framework for AI agents
- **Knowledgebase Integration**: Workflows can access multiple knowledgebases
- **Async Support**: Full async/await support for message processing

### 📝 Document Ingestion

- **IngestKnowledgeBase**: Automated document processing and article creation
- **Fact Extraction**: Extract structured information from documents
- **Semantic Embeddings**: Automatic embedding generation for search

## Quick Start

### 1. Create a Workflow

```python
from papa.apps.ergo.workflows import create_default_workflow
from papa.apps.ergo.models import Knowledgebase

# Create knowledgebases
kb = Knowledgebase.objects.create(
    name="My Knowledgebase",
    description="Information about my domain"
)

# Create a workflow
workflow = create_default_workflow(
    name="My Assistant",
    description="A helpful AI assistant",
    instructions="You are a helpful assistant that can search knowledgebases.",
    knowledgebases=[kb]
)
```

### 2. Create a User Chat

```python
from papa.apps.ergo.workflows import create_user_chat
from django.contrib.auth import get_user_model

User = get_user_model()
user = User.objects.get(username="myuser")

# Create a chat for the user
chat = create_user_chat(
    user=user,
    workflow=workflow,
    title="My Chat Session"
)
```

### 3. Process Messages

```python
from papa.apps.ergo.workflows import process_chat_message

# Process a user message
result = await process_chat_message(chat, "What is Python?")

if result.success:
    print(f"Assistant: {result.content}")
    print(f"Tools used: {len(result.tool_calls)}")
else:
    print(f"Error: {result.error}")
```

### 4. Access Chat History

```python
# Get all messages in the chat
messages = chat.get_messages()

# Get recent context (excluding system messages)
context_messages = chat.get_context_messages(limit=10)

# Add messages manually
chat.add_message(
    message_type="user_input",
    content="Hello!",
    role="user"
)
```

## Models

### UserChat

- `user`: ForeignKey to User model
- `workflow`: ForeignKey to Workflow model
- `title`: Chat title
- `is_active`: Whether the chat is active
- `metadata`: JSON field for additional data

### ChatMessage

- `chat`: ForeignKey to UserChat
- `message_type`: Type of message (user_input, assistant_message, etc.)
- `role`: Role of sender (user, assistant, system, tool)
- `content`: Message content
- `metadata`: JSON field for tool calls, responses, etc.

### Workflow

- `name`: Human-readable name
- `description`: What the workflow does
- `instructions`: System instructions for AI agent
- `knowledgebases`: ManyToMany to Knowledgebase
- `tools_config`: JSON configuration for available tools

### Knowledgebase

- `name`: Knowledgebase name
- `description`: Description
- `owner_id`: Optional owner identifier
- `articles`: Related articles

### Article

- `knowledgebase`: ForeignKey to Knowledgebase
- `hierarchy_code`: Position in knowledgebase (e.g., "0", "1A", "B2")
- `title`: Article title
- `content`: Article content with automatic embedding generation
- `embedding`: Vector field for semantic search
- `summary`: Auto-generated summary

## Workflow Engine

The workflow engine provides a self-contained system for processing chat messages:

```python
from papa.apps.ergo.workflows import WorkflowEngine, Tool

# Create custom tools
class MyCustomTool(Tool):
    def __init__(self):
        super().__init__("my_tool", "Description of my tool")

    async def execute(self, context, **kwargs):
        # Tool implementation
        return "Tool result"

# Register tools with the engine
workflow_engine = WorkflowEngine()
workflow_engine.register_tool(MyCustomTool())
```

## Message Types

- `user_input`: Messages from the user
- `assistant_message`: Responses from the AI assistant
- `tool_request`: Requests to use tools
- `tool_response`: Results from tool execution
- `system_message`: System-level messages
- `error`: Error messages

## Examples

See the `examples/` directory for complete working examples:

- `chat_example.py`: Complete chat system demonstration
- Test files in `proprietary/` for detailed usage patterns

## Testing

Run the tests to verify functionality:

```bash
# Run all ergo tests
dc run django_shell pytest papa/apps/ergo/proprietary/ -v

# Run specific test files
dc run django_shell pytest papa/apps/ergo/proprietary/test_chat.py -v
dc run django_shell pytest papa/apps/ergo/proprietary/test_ingest.py -v
```

## Architecture

Ergo is designed to be self-contained and eventually extractable as a standalone Django library. Key design principles:

1. **No External Dependencies**: Workflow engine doesn't depend on external agent libraries
2. **Django ORM Integration**: Full use of Django's ORM capabilities
3. **Async Support**: Native async/await support throughout
4. **Extensible**: Tool system allows easy extension
5. **Testable**: Comprehensive test coverage with dependency injection

## Migration from Previous Version

The new chat system is backward compatible with existing Conversation/Message models. The new UserChat/ChatMessage models provide additional functionality:

- Message typing (user_input, assistant_message, etc.)
- Workflow association
- Tool call tracking
- Enhanced metadata support

## Future Enhancements

- LLM integration for response generation
- Advanced tool chaining
- Streaming responses
- Multi-agent workflows
- Enhanced search capabilities
