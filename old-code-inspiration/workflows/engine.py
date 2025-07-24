"""
Workflow Engine for Ergo

This module provides a self-contained workflow system for processing chat messages
with AI agents, without dependencies on external agent libraries.
"""

import asyncio
import json
from typing import Any, Dict, List, Optional, Protocol, Union
from dataclasses import dataclass, field
from abc import ABC, abstractmethod
import logging

from django.contrib.auth import get_user_model
from django.db import transaction
from asgiref.sync import sync_to_async

from ..models import Workflow, UserChat, ChatMessage, MessageType, MessageRole
from ..models.kb import Knowledgebase

User = get_user_model()
logger = logging.getLogger(__name__)


@dataclass
class WorkflowContext:
    """Context for workflow execution."""

    user: User
    chat: UserChat
    workflow: Workflow
    knowledgebases: List[Knowledgebase] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowResult:
    """Result of workflow execution."""

    success: bool
    message: str
    content: Optional[str] = None
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


class Tool(ABC):
    """Base class for workflow tools."""

    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description

    @abstractmethod
    async def execute(self, context: WorkflowContext, **kwargs) -> Any:
        """Execute the tool with given context and arguments."""
        pass

    def get_schema(self) -> Dict[str, Any]:
        """Get the tool's schema for documentation."""
        return {
            "name": self.name,
            "description": self.description,
            "type": self.__class__.__name__,
        }


class KnowledgebaseSearchTool(Tool):
    """Tool for searching knowledgebases."""

    def __init__(self):
        super().__init__(
            name="search_knowledgebase",
            description="Search knowledgebases for relevant information",
        )

    async def execute(
        self, context: WorkflowContext, query: str, top_k: int = 5
    ) -> List[Dict[str, Any]]:
        """Search knowledgebases for relevant content."""
        results = []

        for kb in context.knowledgebases:
            try:
                # Use the hybrid search from the Article model, force evaluation in thread
                articles_qs = kb.articles.hybrid_search(query)
                articles = await sync_to_async(lambda: list(articles_qs[:top_k]))()
                for article in articles:
                    results.append(
                        {
                            "knowledgebase": kb.name,
                            "title": article.title,
                            "content": article.content,
                            "hierarchy_code": article.hierarchy_code,
                            "summary": article.summary,
                        }
                    )
            except Exception as e:
                logger.error(f"Error searching knowledgebase {kb.name}: {e}")

        return results


class WorkflowEngine:
    """Main workflow engine for processing chat messages."""

    def __init__(self):
        self.tools: Dict[str, Tool] = {}
        self._register_default_tools()

    def _register_default_tools(self):
        """Register default tools."""
        self.register_tool(KnowledgebaseSearchTool())

    def register_tool(self, tool: Tool):
        """Register a new tool with the workflow engine."""
        self.tools[tool.name] = tool

    async def process_message(
        self,
        chat: UserChat,
        user_message: str,
        context_messages: Optional[List[ChatMessage]] = None,
    ) -> WorkflowResult:
        """
        Process a user message through the workflow.

        Args:
            chat: The user chat to process the message in
            user_message: The user's input message
            context_messages: Optional list of previous messages for context

        Returns:
            WorkflowResult with the processing result
        """
        try:
            # Create workflow context
            context = await self._create_context(chat)

            # Add user message to chat
            user_msg = await sync_to_async(chat.add_message)(
                message_type=MessageType.USER_INPUT,
                content=user_message,
                role=MessageRole.USER,
            )

            # Process through workflow
            result = await self._execute_workflow(
                context, user_message, context_messages
            )

            # Add assistant response to chat
            if result.success and result.content:
                assistant_msg = await sync_to_async(chat.add_message)(
                    message_type=MessageType.ASSISTANT_MESSAGE,
                    content=result.content,
                    role=MessageRole.ASSISTANT,
                    metadata=result.metadata,
                )

                # Add tool calls if any
                for tool_call in result.tool_calls:
                    await sync_to_async(assistant_msg.add_tool_call)(
                        tool_name=tool_call.get("tool_name"),
                        arguments=tool_call.get("arguments", {}),
                        result=tool_call.get("result"),
                    )

            return result

        except Exception as e:
            logger.error(f"Error processing message: {e}")
            error_msg = await sync_to_async(chat.add_message)(
                message_type=MessageType.ERROR,
                content=f"Error processing message: {str(e)}",
                role=MessageRole.ASSISTANT,
            )

            return WorkflowResult(
                success=False, message="Error processing message", error=str(e)
            )

    async def _create_context(self, chat: UserChat) -> WorkflowContext:
        """Create workflow context from chat."""
        # Get the workflow with prefetch to avoid async issues
        workflow = await sync_to_async(lambda: chat.workflow)()
        knowledgebases = await sync_to_async(
            lambda: list(workflow.knowledgebases.all())
        )()

        return WorkflowContext(
            user=await sync_to_async(lambda: chat.user)(),
            chat=chat,
            workflow=workflow,
            knowledgebases=knowledgebases,
            metadata=await sync_to_async(workflow.get_tools_config)(),
        )

    async def _execute_workflow(
        self,
        context: WorkflowContext,
        user_message: str,
        context_messages: Optional[List[ChatMessage]] = None,
    ) -> WorkflowResult:
        """
        Execute the workflow logic.

        This is a simplified implementation. In a full system, this would:
        1. Parse the workflow instructions
        2. Determine which tools to use
        3. Execute tools as needed
        4. Generate AI response

        For now, we'll implement a basic search-and-respond pattern.
        """
        try:
            # Get relevant context from previous messages
            conversation_context = ""
            if context_messages:
                recent_messages = context_messages[-5:]  # Last 5 messages
                conversation_context = "\n".join(
                    [f"{msg.role}: {msg.content}" for msg in recent_messages]
                )

            # Search knowledgebases for relevant information
            search_tool = self.tools.get("search_knowledgebase")
            search_results = []

            if search_tool and context.knowledgebases:
                search_results = await search_tool.execute(
                    context, query=user_message, top_k=3
                )

            # Generate response based on search results and workflow instructions
            response_content = await self._generate_response(
                context, user_message, search_results, conversation_context
            )

            # Prepare tool calls metadata
            tool_calls = []
            if search_results:
                tool_calls.append(
                    {
                        "tool_name": "search_knowledgebase",
                        "arguments": {"query": user_message, "top_k": 3},
                        "result": f"Found {len(search_results)} relevant articles",
                    }
                )

            return WorkflowResult(
                success=True,
                message="Message processed successfully",
                content=response_content,
                tool_calls=tool_calls,
                metadata={
                    "search_results_count": len(search_results),
                    "knowledgebases_searched": [
                        kb.name for kb in context.knowledgebases
                    ],
                },
            )

        except Exception as e:
            logger.error(f"Error executing workflow: {e}")
            return WorkflowResult(
                success=False, message="Error executing workflow", error=str(e)
            )

    async def _generate_response(
        self,
        context: WorkflowContext,
        user_message: str,
        search_results: List[Dict[str, Any]],
        conversation_context: str,
    ) -> str:
        """
        Generate a response based on the workflow instructions and search results.

        This implementation tries to use OpenAI Agents if available, otherwise falls back
        to a simple template-based response.
        """
        workflow = context.workflow

        # Try to use OpenAI Agents if available
        try:
            from .openai_agent import (
                OpenAIAgentConfig,
                process_message_with_openai_agent,
            )

            # Create OpenAI Agent config
            config = OpenAIAgentConfig(
                model="gpt-4o-mini",
                system_prompt=workflow.instructions,
                temperature=0.7,
            )

            # Process with OpenAI Agent
            result = await process_message_with_openai_agent(
                chat=context.chat,
                user_message=user_message,
                context_messages=None,  # Could pass conversation_context here
                config=config,
            )

            if result.success and result.content:
                return result.content
            else:
                logger.warning(f"OpenAI Agent failed: {result.error}")

        except ImportError:
            logger.info("OpenAI Agents not available, using fallback response")
        except Exception as e:
            logger.error(f"Error using OpenAI Agent: {e}")

        # Fallback to simple template-based response
        if search_results:
            response_parts = [
                f"Based on your question about '{user_message}', I found some relevant information:"
            ]

            for i, result in enumerate(search_results[:3], 1):
                kb_name = result.get("knowledgebase", "Unknown")
                title = result.get("title", "Untitled")
                summary = result.get("summary", "")

                response_parts.append(f"\n{i}. From {kb_name}: {title}")
                if summary:
                    response_parts.append(f"   Summary: {summary[:200]}...")

            response_parts.append(
                f"\n\nThis response was generated using the '{workflow.name}' workflow."
            )
            return "\n".join(response_parts)
        else:
            return (
                f"I don't have specific information about '{user_message}' in my knowledgebases. "
                f"This response was generated using the '{workflow.name}' workflow."
            )


# Global workflow engine instance
workflow_engine = WorkflowEngine()


async def process_chat_message(
    chat: UserChat,
    user_message: str,
    context_messages: Optional[List[ChatMessage]] = None,
) -> WorkflowResult:
    """
    Convenience function to process a chat message.

    Args:
        chat: The user chat to process the message in
        user_message: The user's input message
        context_messages: Optional list of previous messages for context

    Returns:
        WorkflowResult with the processing result
    """
    return await workflow_engine.process_message(chat, user_message, context_messages)


def create_default_workflow(
    name: str,
    description: str,
    instructions: str,
    knowledgebases: Optional[List[Knowledgebase]] = None,
) -> Workflow:
    """
    Create a default workflow with basic configuration.

    Args:
        name: Name of the workflow
        description: Description of what the workflow does
        instructions: System instructions for the AI agent
        knowledgebases: Optional list of knowledgebases to associate

    Returns:
        Created Workflow instance
    """
    workflow = Workflow.objects.create(
        name=name,
        description=description,
        instructions=instructions,
        tools_config={"search_knowledgebase": {"enabled": True, "max_results": 5}},
    )

    if knowledgebases:
        workflow.knowledgebases.set(knowledgebases)

    return workflow


def create_user_chat(
    user: User, workflow: Workflow, title: str = "New Chat"
) -> UserChat:
    """
    Create a new chat for a user with a specific workflow.

    Args:
        user: The user who owns the chat
        workflow: The workflow to use for processing messages
        title: Title for the chat

    Returns:
        Created UserChat instance
    """
    return UserChat.objects.create(user=user, workflow=workflow, title=title)
