"""
Workflow engine for Django Ergo.

Provides a framework for processing chat messages through AI workflows
with tool execution, state management, and pause/resume capabilities.
"""

import json
import os
from dataclasses import dataclass
from typing import Any

# OpenAI is a strict requirement - no graceful fallbacks
# The system is designed around OpenAI Python agents and requires OpenAI API access
import openai
from django.contrib.auth import get_user_model
from django.dispatch import Signal

from django_ergo.models import ChatMessage
from django_ergo.models import MessageRole
from django_ergo.models import MessageType
from django_ergo.models import UserChat
from django_ergo.models import Workflow
from django_ergo.settings import api_settings
from django_ergo.tools import tool_registry

User = get_user_model()

# Workflow approval signals
tool_approval_requested = Signal()  # Fired when tool approval is needed
workflow_paused = Signal()  # Fired when workflow is paused for approval
workflow_resumed = Signal()  # Fired when workflow resumes after approval


@dataclass
class WorkflowContext:
    """Context for workflow execution."""

    user: User
    chat: UserChat
    workflow: Workflow
    current_message: ChatMessage | None = None
    state: dict[str, Any] = None

    def __post_init__(self):
        if self.state is None:
            self.state = {}


@dataclass
class ApprovalRequest:
    """Represents a tool approval request."""

    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    context: WorkflowContext
    serialized_state: dict[str, Any]


class WorkflowEngine:
    """
    Main workflow engine for processing chat messages.

    Handles:
    - Message processing through AI agents
    - Tool execution with approval system
    - Workflow state persistence and resumption
    - Context serialization for pause/resume
    - Tool approval events and whitelisting
    """

    def __init__(self):
        self.model = api_settings.OPENAI_MODEL
        self.temperature = api_settings.OPENAI_TEMPERATURE
        self.max_tokens = api_settings.OPENAI_MAX_TOKENS
        self.timeout = api_settings.OPENAI_TIMEOUT

        # Get API key from environment or settings - fail if not found
        api_key = os.getenv("OPENAI_API_KEY") or api_settings.OPENAI_API_KEY
        if not api_key:
            msg = (
                "OpenAI API key is required but not found. "
                "Set OPENAI_API_KEY environment variable or configure it in DJANGO_ERGO settings."
            )
            raise ValueError(msg)

        try:
            self.openai_client = openai.OpenAI(api_key=api_key, timeout=self.timeout)
        except Exception as e:  # noqa: BLE001
            msg = f"Failed to initialize OpenAI client: {e}"
            raise ValueError(msg) from e

    def process_message(
        self,
        chat: UserChat,
        message_content: str,
        resume_from_message: ChatMessage | None = None,
    ) -> ChatMessage:
        """
        Process a user message through the workflow.

        Args:
            chat: The user chat
            message_content: The user's message content
            resume_from_message: Optional message to resume from (for approval flows)

        Returns:
            ChatMessage: The response message (may be approval request)
        """
        context = WorkflowContext(
            user=chat.user,
            chat=chat,
            workflow=chat.workflow,
            state=chat.get_workflow_state(),
        )

        # Check if we're resuming from an approval
        if resume_from_message:
            return self._resume_from_approval(
                context, resume_from_message, message_content
            )

        # Create user message
        user_message = chat.add_message(
            message_type=MessageType.USER_INPUT,
            content=message_content,
            role=MessageRole.USER,
        )
        context.current_message = user_message

        try:
            return self._process_with_agent(context)
        except Exception as e:  # noqa: BLE001
            return chat.add_message(
                message_type=MessageType.ERROR,
                content=f"Workflow error: {e!s}",
                role=MessageRole.ASSISTANT,
                metadata={"error_type": "workflow_error", "original_error": str(e)},
            )

    def approve_tool_execution(
        self,
        chat: UserChat,
        approval_message: ChatMessage,
        approved_tools: list[str],
        denied_tools: list[str] | None = None,
    ) -> ChatMessage:
        """
        Approve or deny tool execution and resume workflow.

        Args:
            chat: The user chat
            approval_message: The message containing approval request
            approved_tools: List of tool call IDs that are approved
            denied_tools: List of tool call IDs that are denied

        Returns:
            ChatMessage: The workflow continuation response
        """
        if denied_tools is None:
            denied_tools = []

        # Create approval response message
        approval_response = chat.add_message(
            message_type=MessageType.TOOL_APPROVAL_RESPONSE,
            content=f"Approved: {len(approved_tools)} tools, Denied: {len(denied_tools)} tools",
            role=MessageRole.USER,
            metadata={
                "approved_tools": approved_tools,
                "denied_tools": denied_tools,
                "original_approval_id": str(approval_message.id),
            },
        )

        # Resume workflow from saved context
        return self._resume_from_approval_response(
            chat, approval_message, approval_response
        )

    def get_tool_whitelist(self, workflow: Workflow) -> list[str]:
        """
        Get the tool whitelist for a workflow.
        Apps can configure which tools are pre-approved.

        Args:
            workflow: The workflow to check

        Returns:
            List[str]: List of pre-approved tool names
        """
        tools_config = workflow.tools_config or {}
        return tools_config.get("approved_tools", [])

    def is_tool_whitelisted(self, workflow: Workflow, tool_name: str) -> bool:
        """
        Check if a tool is whitelisted for automatic approval.

        Args:
            workflow: The workflow to check
            tool_name: Name of the tool

        Returns:
            bool: True if tool is whitelisted
        """
        whitelist = self.get_tool_whitelist(workflow)
        return tool_name in whitelist

    def serialize_workflow_context(
        self, context: WorkflowContext, openai_messages: list[dict]
    ) -> dict[str, Any]:
        """
        Serialize workflow context for pause/resume functionality.

        Args:
            context: Current workflow context
            openai_messages: OpenAI conversation messages

        Returns:
            Dict containing serialized context
        """
        return {
            "workflow_id": str(context.workflow.id),
            "chat_id": str(context.chat.id),
            "user_id": str(context.user.id),
            "openai_messages": openai_messages,
            "workflow_state": context.state,
            "model_config": {
                "model": self.model,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            },
            "timestamp": context.current_message.created_at.isoformat()
            if context.current_message
            else None,
        }

    def _process_with_agent(self, context: WorkflowContext) -> ChatMessage:
        """
        Process the message with an OpenAI agent.

        Args:
            context: Workflow context

        Returns:
            ChatMessage: The assistant's response message (may be approval request)
        """
        # Build conversation history for OpenAI (including system message)
        messages = self._build_openai_conversation_history(context)

        # Get available tools for this workflow
        tools = self._get_workflow_tools(context.workflow)

        try:
            # Make OpenAI API call
            response = self.openai_client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools if tools else None,
                tool_choice="auto" if tools else None,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )

            message = response.choices[0].message

            # Handle tool calls
            if message.tool_calls:
                tool_response = self._handle_tool_calls(context, message, response)

                # If it's an approval request, return the approval message
                if tool_response.get("approval_request"):
                    return context.chat.messages.filter(
                        message_type=MessageType.TOOL_APPROVAL_REQUEST
                    ).last()

                # Otherwise continue with tool results and create assistant message
                return context.chat.add_message(
                    message_type=MessageType.ASSISTANT_MESSAGE,
                    content=tool_response["content"],
                    role=MessageRole.ASSISTANT,
                    metadata=tool_response.get("metadata", {}),
                )

            # Handle regular response - create assistant message
            return context.chat.add_message(
                message_type=MessageType.ASSISTANT_MESSAGE,
                content=message.content
                or "I apologize, but I couldn't generate a response.",
                role=MessageRole.ASSISTANT,
                metadata={
                    "model": self.model,
                    "usage": response.usage.model_dump() if response.usage else {},
                },
            )

        except Exception as e:  # noqa: BLE001
            return context.chat.add_message(
                message_type=MessageType.ERROR,
                content=f"I encountered an error: {e!s}",
                role=MessageRole.ASSISTANT,
                metadata={
                    "error": str(e),
                    "model": self.model,
                    "error_type": "agent_processing_error",
                },
            )

    def _build_openai_conversation_history(
        self, context: WorkflowContext
    ) -> list[dict[str, Any]]:
        """
        Build conversation history for OpenAI API calls.

        Args:
            context: Workflow context

        Returns:
            List of messages in OpenAI format including system message
        """
        messages = []

        # Add system message with workflow instructions
        messages.append({"role": "system", "content": context.workflow.instructions})

        # Add recent chat messages
        recent_messages = context.chat.get_context_messages(limit=20)

        for msg in reversed(recent_messages):  # Reverse to get chronological order
            if msg.message_type == MessageType.USER_INPUT:
                messages.append({"role": "user", "content": msg.content})
            elif msg.message_type == MessageType.ASSISTANT_MESSAGE:
                messages.append({"role": "assistant", "content": msg.content})
            elif msg.message_type == MessageType.TOOL_RESPONSE:
                # Add tool response
                messages.append(
                    {
                        "role": "tool",
                        "content": msg.content,
                        "tool_call_id": msg.get_metadata("tool_call_id", ""),
                    }
                )

        return messages

    def _build_conversation_history(self, context: WorkflowContext) -> list[dict]:
        """
        Build conversation history for context serialization.

        Args:
            context: Workflow context

        Returns:
            List of conversation messages in OpenAI format
        """
        messages = []

        # Get recent messages from chat
        chat_messages = context.chat.get_context_messages(limit=20)

        for msg in reversed(chat_messages):  # Reverse to get chronological order
            if msg.message_type == MessageType.USER_INPUT:
                messages.append({"role": "user", "content": msg.content})
            elif msg.message_type == MessageType.ASSISTANT_MESSAGE:
                messages.append({"role": "assistant", "content": msg.content})
            elif msg.message_type == MessageType.TOOL_RESPONSE:
                messages.append(
                    {
                        "role": "tool",
                        "content": msg.content,
                        "tool_call_id": msg.get_metadata("tool_call_id"),
                    }
                )

        return messages

    def _get_workflow_tools(self, workflow: Workflow) -> list[dict[str, Any]] | None:
        """
        Get tools available to the workflow in OpenAI format.

        Args:
            workflow: The workflow

        Returns:
            List of tools in OpenAI format, or None if no tools
        """
        tools_config = workflow.get_tools_config()
        if not tools_config:
            return None

        tools = []

        # Get tools from registry
        for tool_name in tools_config.get("enabled_tools", []):
            tool_config = tool_registry.get_tool(tool_name)
            if tool_config:
                tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool_config.name,
                            "description": tool_config.description,
                            "parameters": {
                                "type": "object",
                                "properties": tool_config.parameters,
                                "required": [
                                    name
                                    for name, param in tool_config.parameters.items()
                                    if param.get("required", False)
                                ],
                            },
                        },
                    }
                )

        return tools if tools else None

    def _handle_tool_calls(
        self, context: WorkflowContext, message: Any, response: Any
    ) -> dict[str, Any]:
        """
        Handle tool calls from the AI agent with approval workflow support.

        Args:
            context: Workflow context
            message: OpenAI message with tool calls
            response: Full OpenAI response

        Returns:
            Dictionary with response content and metadata
        """
        tool_results = []
        pending_approvals = []

        for tool_call in message.tool_calls:
            tool_name = tool_call.function.name
            try:
                arguments = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                arguments = {}

            # Check tool configuration
            tool_config = tool_registry.get_tool(tool_name)

            # Check if tool is whitelisted for auto-approval
            is_whitelisted = self.is_tool_whitelisted(context.workflow, tool_name)

            # Determine if approval is needed
            needs_approval = (
                tool_config and tool_config.requires_approval and not is_whitelisted
            )

            if needs_approval:
                # Save tool call for approval
                pending_approvals.append(
                    {
                        "tool_call_id": tool_call.id,
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "description": tool_config.description
                        if tool_config
                        else "Unknown tool",
                        "requires_approval": True,
                    }
                )
                continue

            # Execute tool (auto-approved or whitelisted)
            try:
                result = tool_registry.execute_tool(
                    name=tool_name,
                    user=context.user,
                    arguments=arguments,
                    approved=True,
                )

                tool_results.append({"tool_call_id": tool_call.id, "result": result})

                # Create tool response message
                context.chat.add_message(
                    message_type=MessageType.TOOL_RESPONSE,
                    content=json.dumps(result),
                    role=MessageRole.TOOL,
                    metadata={
                        "tool_name": tool_name,
                        "tool_call_id": tool_call.id,
                        "arguments": arguments,
                        "auto_approved": not needs_approval,
                        "whitelisted": is_whitelisted,
                    },
                )

            except Exception as e:  # noqa: BLE001
                error_result = {"error": str(e)}
                tool_results.append(
                    {"tool_call_id": tool_call.id, "result": error_result}
                )

        # If there are pending approvals, create approval request
        if pending_approvals:
            return self._create_approval_request(
                context, message, response, pending_approvals
            )

        # Continue conversation with tool results
        if tool_results:
            return self._continue_with_tool_results(context, tool_results)

        return {
            "content": message.content or "Tool execution completed.",
            "metadata": {"tool_calls_executed": len(tool_results), "model": self.model},
        }

    def _create_approval_request(
        self,
        context: WorkflowContext,
        message: Any,
        response: Any,
        pending_approvals: list[dict],
    ) -> dict[str, Any]:
        """
        Create an approval request with serialized context for pause/resume.

        Args:
            context: Workflow context
            message: OpenAI message with tool calls
            response: Full OpenAI response
            pending_approvals: List of tools pending approval

        Returns:
            Dictionary with approval request content and serialized context
        """
        # Build conversation history for serialization
        messages_history = self._build_conversation_history(context)

        # Serialize context for pause/resume
        serialized_context = self.serialize_workflow_context(context, messages_history)

        # Create approval request message
        approval_content = self._format_approval_request(pending_approvals)

        approval_message = context.chat.add_message(
            message_type=MessageType.TOOL_APPROVAL_REQUEST,
            content=approval_content,
            role=MessageRole.ASSISTANT,
            metadata={
                "pending_approvals": pending_approvals,
                "requires_user_approval": True,
                "tool_call_count": len(pending_approvals),
            },
        )

        # Save serialized context to approval message
        approval_message.save_agent_context(serialized_context)

        # Save workflow state
        context.chat.save_workflow_state(context.state)

        # Fire signals for external handling
        tool_approval_requested.send(
            sender=self.__class__,
            context=context,
            pending_approvals=pending_approvals,
            approval_message=approval_message,
        )

        workflow_paused.send(
            sender=self.__class__, context=context, approval_message=approval_message
        )

        return {
            "content": approval_content,
            "metadata": {
                "pending_approvals": pending_approvals,
                "requires_user_approval": True,
                "approval_message_id": str(approval_message.id),
                "serialized_context": serialized_context,
            },
            "approval_request": True,
        }

    def _format_approval_request(self, pending_approvals: list[dict]) -> str:
        """
        Format approval request content for user display.

        Args:
            pending_approvals: List of tools pending approval

        Returns:
            Formatted approval request message
        """
        lines = ["🔒 **Tool Approval Required**", ""]
        lines.append("The following tools require your approval before execution:")
        lines.append("")

        for i, approval in enumerate(pending_approvals, 1):
            tool_name = approval["tool_name"]
            description = approval.get("description", "No description available")
            arguments = approval.get("arguments", {})

            lines.append(f"**{i}. {tool_name}**")
            lines.append(f"   - Description: {description}")
            lines.append(f"   - Arguments: {json.dumps(arguments, indent=2)}")
            lines.append("")

        lines.append("Please review and approve or deny these tool executions.")
        lines.append("Use the approval interface or respond with your decision.")

        return "\n".join(lines)

    def _continue_with_tool_results(
        self, context: WorkflowContext, tool_results: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """
        Continue conversation after tool execution.

        Args:
            context: Workflow context
            tool_results: Results from tool execution

        Returns:
            Dictionary with final response
        """
        # Build messages including tool results
        messages = self._build_conversation_history(context)

        # Add tool results
        for result in tool_results:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": result["tool_call_id"],
                    "content": json.dumps(result["result"]),
                }
            )

        # Get final response
        response = self.openai_client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )

        message = response.choices[0].message

        return {
            "content": message.content or "",
            "metadata": {
                "model": self.model,
                "tool_results_processed": len(tool_results),
                "usage": response.usage.model_dump() if response.usage else {},
            },
        }

    def _resume_from_approval(
        self,
        context: WorkflowContext,
        resume_message: ChatMessage,
        message_content: str,
    ) -> ChatMessage:
        """
        Resume workflow from an approval message.
        """
        # Deserialize context from the message
        try:
            serialized_context = json.loads(resume_message.content)
            workflow_id = serialized_context.get("workflow_id")
            chat_id = serialized_context.get("chat_id")
            user_id = serialized_context.get("user_id")
            openai_messages = serialized_context.get("openai_messages")
            workflow_state = serialized_context.get("workflow_state")

            if not all(
                [workflow_id, chat_id, user_id, openai_messages, workflow_state]
            ):
                msg = "Invalid serialized context in approval message."
                raise ValueError(msg)  # noqa: TRY301

            # Reconstruct context
            context.workflow = Workflow.objects.get(id=workflow_id)
            context.chat = UserChat.objects.get(id=chat_id)
            context.user = User.objects.get(id=user_id)
            context.state = workflow_state
            context.current_message = resume_message

            # Re-add messages to context.chat
            for msg_data in openai_messages:
                if msg_data["role"] == "user":
                    context.chat.add_message(
                        message_type=MessageType.USER_INPUT,
                        content=msg_data["content"],
                        role=MessageRole.USER,
                    )
                elif msg_data["role"] == "assistant":
                    context.chat.add_message(
                        message_type=MessageType.ASSISTANT_MESSAGE,
                        content=msg_data["content"],
                        role=MessageRole.ASSISTANT,
                    )
                elif msg_data["role"] == "tool":
                    context.chat.add_message(
                        message_type=MessageType.TOOL_RESPONSE,
                        content=msg_data["content"],
                        role=MessageRole.TOOL,
                        metadata={"tool_call_id": msg_data["tool_call_id"]},
                    )

            # Update workflow state in the chat
            context.chat.update_workflow_state(context.state)

            # Create a new user message for the resumed workflow
            user_message = context.chat.add_message(
                message_type=MessageType.USER_INPUT,
                content=message_content,
                role=MessageRole.USER,
            )
            context.current_message = user_message

            return self._process_with_agent(context)
        except Exception as e:  # noqa: BLE001
            return context.chat.add_message(
                message_type=MessageType.ERROR,
                content=f"Error resuming workflow from approval: {e!s}",
                role=MessageRole.ASSISTANT,
                metadata={"error_type": "resume_error", "original_error": str(e)},
            )

    def _resume_from_approval_response(
        self,
        chat: UserChat,
        approval_message: ChatMessage,
        approval_response: ChatMessage,
    ) -> ChatMessage:
        """
        Resume workflow from an approval response message.
        """
        # Deserialize context from the message
        try:
            serialized_context = json.loads(approval_response.content)
            workflow_id = serialized_context.get("workflow_id")
            chat_id = serialized_context.get("chat_id")
            user_id = serialized_context.get("user_id")
            openai_messages = serialized_context.get("openai_messages")
            workflow_state = serialized_context.get("workflow_state")

            if not all(
                [workflow_id, chat_id, user_id, openai_messages, workflow_state]
            ):
                msg = "Invalid serialized context in approval response message."
                raise ValueError(msg)  # noqa: TRY301

            # Reconstruct context
            context = WorkflowContext(
                user=User.objects.get(id=user_id),
                chat=UserChat.objects.get(id=chat_id),
                workflow=Workflow.objects.get(id=workflow_id),
                state=workflow_state,
                current_message=approval_response,
            )

            # Re-add messages to context.chat
            for msg_data in openai_messages:
                if msg_data["role"] == "user":
                    context.chat.add_message(
                        message_type=MessageType.USER_INPUT,
                        content=msg_data["content"],
                        role=MessageRole.USER,
                    )
                elif msg_data["role"] == "assistant":
                    context.chat.add_message(
                        message_type=MessageType.ASSISTANT_MESSAGE,
                        content=msg_data["content"],
                        role=MessageRole.ASSISTANT,
                    )
                elif msg_data["role"] == "tool":
                    context.chat.add_message(
                        message_type=MessageType.TOOL_RESPONSE,
                        content=msg_data["content"],
                        role=MessageRole.TOOL,
                        metadata={"tool_call_id": msg_data["tool_call_id"]},
                    )

            # Update workflow state in the chat
            context.chat.update_workflow_state(context.state)

            # Create a new user message for the resumed workflow
            user_message = context.chat.add_message(
                message_type=MessageType.USER_INPUT,
                content=approval_message.content,
                role=MessageRole.USER,
            )
            context.current_message = user_message

            return self._process_with_agent(context)
        except Exception as e:  # noqa: BLE001
            return chat.add_message(
                message_type=MessageType.ERROR,
                content=f"Error resuming workflow from approval response: {e!s}",
                role=MessageRole.ASSISTANT,
                metadata={"error_type": "resume_error", "original_error": str(e)},
            )


# Global workflow engine instance
workflow_engine = WorkflowEngine()
