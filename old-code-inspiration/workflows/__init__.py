# OpenAI Agents integration (optional)
try:
    from .openai_agent import ErgoOpenAITool
    from .openai_agent import OpenAIAgentConfig
    from .openai_agent import OpenAIAgentWorkflow
    from .openai_agent import create_openai_agent_workflow
    from .openai_agent import process_message_with_openai_agent

    OPENAI_AGENTS_AVAILABLE = True
except ImportError:
    OPENAI_AGENTS_AVAILABLE = False
    OpenAIAgentConfig = None
    OpenAIAgentWorkflow = None
    ErgoOpenAITool = None
    create_openai_agent_workflow = None
    process_message_with_openai_agent = None
