from .engine import (
    WorkflowEngine,
    WorkflowContext,
    WorkflowResult,
    Tool,
    KnowledgebaseSearchTool,
    workflow_engine,
    process_chat_message,
    create_default_workflow,
    create_user_chat,
)
from .ingest import IngestKnowledgeBase, FactExtractionAgent

# OpenAI Agents integration (optional)
try:
    from .openai_agent import (
        OpenAIAgentConfig,
        OpenAIAgentWorkflow,
        ErgoOpenAITool,
        create_openai_agent_workflow,
        process_message_with_openai_agent,
    )

    OPENAI_AGENTS_AVAILABLE = True
except ImportError:
    OPENAI_AGENTS_AVAILABLE = False
    OpenAIAgentConfig = None
    OpenAIAgentWorkflow = None
    ErgoOpenAITool = None
    create_openai_agent_workflow = None
    process_message_with_openai_agent = None
