"""
OpenAI Agents Integration for Ergo Workflows

This module provides integration with OpenAI Agents for LLM-powered chat responses.
Based on the OpenAI Agents customer_service example.
"""

import os
import logging
from typing import Any, Dict, List, Optional
from dataclasses import dataclass

try:
    from openai_agents import Agent, AgentConfig, AgentResponse
    from openai_agents.tools import Tool

    OPENAI_AGENTS_AVAILABLE = True
except ImportError:
    OPENAI_AGENTS_AVAILABLE = False
    Agent = None
    AgentConfig = None
    AgentResponse = None
    Tool = None

from ..models import Workflow, UserChat, ChatMessage, MessageType, MessageRole
from .engine import WorkflowContext, WorkflowResult

logger = logging.getLogger(__name__)


@dataclass
class OpenAIAgentConfig:
    """Configuration for OpenAI Agent integration."""

    model: str = "gpt-4o-mini"
    api_key: Optional[str] = None
    system_prompt: Optional[str] = None
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    tools_enabled: bool = True


if Tool is not None:

    class ErgoOpenAITool(Tool):
        """Base class for Ergo tools that can be used with OpenAI Agents."""

        def __init__(self, name: str, description: str, ergo_tool):
            super().__init__(name=name, description=description)
            self.ergo_tool = ergo_tool

        async def run(self, **kwargs) -> str:
            """Run the tool and return the result as a string."""
            try:
                result = await self.ergo_tool.execute(self.context, **kwargs)
                if isinstance(result, list):
                    return str(result)
                elif isinstance(result, dict):
                    return str(result)
                else:
                    return str(result)
            except Exception as e:
                logger.error(f"Error running tool {self.name}: {e}")
                return f"Error: {str(e)}"
else:
    # Fallback class when OpenAI Agents is not available
    class ErgoOpenAITool:
        """Fallback class for Ergo tools when OpenAI Agents is not available."""

        def __init__(self, name: str, description: str, ergo_tool):
            self.name = name
            self.description = description
            self.ergo_tool = ergo_tool

        async def run(self, **kwargs) -> str:
            """Run the tool and return the result as a string."""
            return f"Tool {self.name} not available (OpenAI Agents not installed)"


class OpenAIAgentWorkflow:
    """OpenAI Agent integration for Ergo workflows."""

    def __init__(self, config: OpenAIAgentConfig):
        self.config = config
        self.agent: Optional[Agent] = None
        self._setup_agent()

    def _setup_agent(self):
        """Set up the OpenAI Agent."""
        if not OPENAI_AGENTS_AVAILABLE:
            logger.warning(
                "OpenAI Agents not available. Install with: pip install openai-agents"
            )
            return

        try:
            # Get API key from environment or config
            api_key = self.config.api_key or os.getenv("OPENAI_API_KEY")
            if not api_key:
                logger.error(
                    "OpenAI API key not found. Set OPENAI_API_KEY environment variable."
                )
                return

            # Create agent configuration
            agent_config = AgentConfig(
                model=self.config.model,
                api_key=api_key,
                system_prompt=self.config.system_prompt
                or "You are a helpful AI assistant.",
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )

            # Create the agent
            self.agent = Agent(agent_config)
            logger.info(f"OpenAI Agent initialized with model: {self.config.model}")

        except Exception as e:
            logger.error(f"Error setting up OpenAI Agent: {e}")
            self.agent = None

    def add_ergo_tools(self, ergo_tools: List[Any]):
        """Add Ergo tools to the OpenAI Agent."""
        if not self.agent or not OPENAI_AGENTS_AVAILABLE:
            return

        try:
            for ergo_tool in ergo_tools:
                # Create OpenAI Agent tool wrapper
                openai_tool = ErgoOpenAITool(
                    name=ergo_tool.name,
                    description=ergo_tool.description,
                    ergo_tool=ergo_tool,
                )

                # Add tool to agent
                self.agent.add_tool(openai_tool)
                logger.info(f"Added tool to OpenAI Agent: {ergo_tool.name}")

        except Exception as e:
            logger.error(f"Error adding tools to OpenAI Agent: {e}")

    async def generate_response(
        self,
        user_message: str,
        context_messages: Optional[List[ChatMessage]] = None,
        workflow_context: Optional[WorkflowContext] = None,
    ) -> WorkflowResult:
        """
        Generate a response using the OpenAI Agent.

        Args:
            user_message: The user's input message
            context_messages: Previous messages for context
            workflow_context: Current workflow context

        Returns:
            WorkflowResult with the generated response
        """
        if not self.agent:
            return WorkflowResult(
                success=False,
                message="OpenAI Agent not available",
                error="OpenAI Agent not initialized",
            )

        try:
            # Prepare conversation history
            messages = []

            # Add system message if provided
            if self.config.system_prompt:
                messages.append(
                    {"role": "system", "content": self.config.system_prompt}
                )

            # Add context messages
            if context_messages:
                for msg in context_messages[-10:]:  # Last 10 messages for context
                    messages.append({"role": msg.role, "content": msg.content})

            # Add current user message
            messages.append({"role": "user", "content": user_message})

            # Generate response using OpenAI Agent
            response = await self.agent.run(messages)

            # Extract response content
            if hasattr(response, "content"):
                content = response.content
            elif hasattr(response, "message"):
                content = response.message
            else:
                content = str(response)

            # Extract tool calls if any
            tool_calls = []
            if hasattr(response, "tool_calls") and response.tool_calls:
                for tool_call in response.tool_calls:
                    tool_calls.append(
                        {
                            "tool_name": tool_call.get("name", "unknown"),
                            "arguments": tool_call.get("arguments", {}),
                            "result": tool_call.get("result", ""),
                        }
                    )

            return WorkflowResult(
                success=True,
                message="Response generated successfully",
                content=content,
                tool_calls=tool_calls,
                metadata={
                    "model": self.config.model,
                    "temperature": self.config.temperature,
                    "agent_type": "openai_agents",
                },
            )

        except Exception as e:
            logger.error(f"Error generating response with OpenAI Agent: {e}")
            return WorkflowResult(
                success=False, message="Error generating response", error=str(e)
            )


def create_openai_agent_workflow(
    workflow: Workflow, config: Optional[OpenAIAgentConfig] = None
) -> OpenAIAgentWorkflow:
    """
    Create an OpenAI Agent workflow from an Ergo workflow.

    Args:
        workflow: The Ergo workflow to convert
        config: Optional configuration for the OpenAI Agent

    Returns:
        OpenAIAgentWorkflow instance
    """
    if config is None:
        config = OpenAIAgentConfig()

    # Use workflow instructions as system prompt
    if workflow.instructions:
        config.system_prompt = workflow.instructions

    # Create the OpenAI Agent workflow
    openai_workflow = OpenAIAgentWorkflow(config)

    return openai_workflow


async def process_message_with_openai_agent(
    chat: UserChat,
    user_message: str,
    context_messages: Optional[List[ChatMessage]] = None,
    config: Optional[OpenAIAgentConfig] = None,
) -> WorkflowResult:
    """
    Process a message using OpenAI Agents.

    Args:
        chat: The user chat
        user_message: The user's input message
        context_messages: Previous messages for context
        config: Optional OpenAI Agent configuration

    Returns:
        WorkflowResult with the processing result
    """
    # Create OpenAI Agent workflow
    openai_workflow = create_openai_agent_workflow(chat.workflow, config)

    # Generate response
    result = await openai_workflow.generate_response(
        user_message=user_message, context_messages=context_messages
    )

    return result
