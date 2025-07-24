"""
Workflow engine for Django Ergo.

Provides a framework for processing chat messages through AI workflows
with tool execution, state management, and pause/resume capabilities.
"""

import json
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass
from django.contrib.auth import get_user_model
from django_ergo.models import UserChat, ChatMessage, Workflow, MessageType, MessageRole
from django_ergo.tools import tool_registry
# import openai  # Commented out for development

User = get_user_model()


@dataclass
class WorkflowContext:
    """Context for workflow execution."""
    user: User
    chat: UserChat
    workflow: Workflow
    current_message: Optional[ChatMessage] = None
    state: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.state is None:
            self.state = {}


class WorkflowEngine:
    """
    Main workflow engine for processing chat messages.
    
    Handles:
    - Message processing through AI agents
    - Tool execution with approval system
    - Workflow state persistence and resumption
    - Context serialization for pause/resume
    """
    
    def __init__(self):
        # self.openai_client = openai.OpenAI()  # Commented out for development
        pass
    
    def process_message(
        self, 
        chat: UserChat, 
        message_content: str,
        resume_from_message: Optional[ChatMessage] = None
    ) -> ChatMessage:
        """
        Process a user message through the workflow.
        
        Args:
            chat: The user chat
            message_content: The user's message content
            resume_from_message: Optional message to resume from
            
        Returns:
            The assistant's response message
        """
        # Create user message if not resuming
        if not resume_from_message:
            user_message = chat.add_message(
                message_type=MessageType.USER_INPUT,
                content=message_content,
                role=MessageRole.USER
            )
        else:
            user_message = resume_from_message
        
        # Create workflow context
        context = WorkflowContext(
            user=chat.user,
            chat=chat,
            workflow=chat.workflow,
            current_message=user_message,
            state=chat.get_workflow_state()
        )
        
        try:
            # Process through AI agent
            response = self._process_with_agent(context)
            
            # Create assistant response message
            assistant_message = chat.add_message(
                message_type=MessageType.ASSISTANT_MESSAGE,
                content=response["content"],
                role=MessageRole.ASSISTANT,
                metadata=response.get("metadata", {})
            )
            
            # Save agent context for potential resume
            if "agent_context" in response:
                assistant_message.save_agent_context(response["agent_context"])
            
            return assistant_message
            
        except Exception as e:
            # Create error message
            error_message = chat.add_message(
                message_type=MessageType.ERROR,
                content=f"Error processing message: {str(e)}",
                role=MessageRole.SYSTEM,
                metadata={"error": str(e), "error_type": type(e).__name__}
            )
            return error_message
    
    def _process_with_agent(self, context: WorkflowContext) -> Dict[str, Any]:
        """
        Process the message with an OpenAI agent.
        
        Args:
            context: Workflow context
            
        Returns:
            Dictionary with response content and metadata
        """
        # For development - return a simple response
        return {
            "content": f"This is a development response to: {context.current_message.content}",
            "metadata": {
                "model": "development",
                "development": True
            }
        }
        
        # Actual OpenAI implementation (commented out for development):
        # # Build conversation history
        # messages = self._build_conversation_history(context)
        # 
        # # Get available tools for this workflow
        # tools = self._get_workflow_tools(context.workflow)
        # 
        # # Make OpenAI API call
        # response = self.openai_client.chat.completions.create(
        #     model="gpt-4o-mini",
        #     messages=messages,
        #     tools=tools if tools else None,
        #     tool_choice="auto" if tools else None
        # )
        # 
        # message = response.choices[0].message
        # 
        # # Handle tool calls
        # if message.tool_calls:
        #     return self._handle_tool_calls(context, message, response)
        # 
        # # Regular text response
        # return {
        #     "content": message.content or "",
        #     "metadata": {
        #         "model": "gpt-4o-mini",
        #         "usage": response.usage.model_dump() if response.usage else {}
        #     },
        #     "agent_context": {
        #         "messages": messages,
        #         "last_response": message.model_dump()
        #     }
        # }
    
    def _build_conversation_history(self, context: WorkflowContext) -> List[Dict[str, Any]]:
        """
        Build conversation history for OpenAI API.
        
        Args:
            context: Workflow context
            
        Returns:
            List of messages in OpenAI format
        """
        messages = []
        
        # Add system message with workflow instructions
        messages.append({
            "role": "system",
            "content": context.workflow.instructions
        })
        
        # Add recent chat messages
        recent_messages = context.chat.get_context_messages(limit=20)
        
        for msg in reversed(recent_messages):  # Reverse to get chronological order
            if msg.message_type == MessageType.USER_INPUT:
                messages.append({
                    "role": "user",
                    "content": msg.content
                })
            elif msg.message_type == MessageType.ASSISTANT_MESSAGE:
                messages.append({
                    "role": "assistant",
                    "content": msg.content
                })
            elif msg.message_type == MessageType.TOOL_RESPONSE:
                # Add tool response
                messages.append({
                    "role": "tool",
                    "content": msg.content,
                    "tool_call_id": msg.get_metadata("tool_call_id", "")
                })
        
        return messages
    
    def _get_workflow_tools(self, workflow: Workflow) -> Optional[List[Dict[str, Any]]]:
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
                tools.append({
                    "type": "function",
                    "function": {
                        "name": tool_config.name,
                        "description": tool_config.description,
                        "parameters": {
                            "type": "object",
                            "properties": tool_config.parameters,
                            "required": [
                                name for name, param in tool_config.parameters.items()
                                if param.get("required", False)
                            ]
                        }
                    }
                })
        
        return tools if tools else None
    
    def _handle_tool_calls(
        self, 
        context: WorkflowContext, 
        message: Any, 
        response: Any
    ) -> Dict[str, Any]:
        """
        Handle tool calls from the AI agent.
        
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
            
            # Check if tool requires approval
            tool_config = tool_registry.get_tool(tool_name)
            if tool_config and tool_config.requires_approval:
                # Save tool call for approval
                pending_approvals.append({
                    "tool_call_id": tool_call.id,
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "requires_approval": True
                })
                continue
            
            # Execute tool
            try:
                result = tool_registry.execute_tool(
                    name=tool_name,
                    user=context.user,
                    arguments=arguments,
                    approved=True  # Auto-approved for non-approval tools
                )
                
                tool_results.append({
                    "tool_call_id": tool_call.id,
                    "result": result
                })
                
                # Create tool response message
                context.chat.add_message(
                    message_type=MessageType.TOOL_RESPONSE,
                    content=json.dumps(result),
                    role=MessageRole.TOOL,
                    metadata={
                        "tool_name": tool_name,
                        "tool_call_id": tool_call.id,
                        "arguments": arguments
                    }
                )
                
            except Exception as e:
                error_result = {"error": str(e)}
                tool_results.append({
                    "tool_call_id": tool_call.id,
                    "result": error_result
                })
        
        # If there are pending approvals, return approval request
        if pending_approvals:
            return {
                "content": "Some tools require approval before execution. Please approve the following actions:",
                "metadata": {
                    "pending_approvals": pending_approvals,
                    "requires_user_approval": True
                },
                "agent_context": {
                    "tool_calls": message.tool_calls,
                    "pending_approvals": pending_approvals
                }
            }
        
        # Continue conversation with tool results
        if tool_results:
            # Add tool results to conversation and get final response
            return self._continue_with_tool_results(context, tool_results)
        
        return {
            "content": message.content or "Tool execution completed.",
            "metadata": {
                "tool_calls_executed": len(tool_results),
                "model": "gpt-4o-mini"
            }
        }
    
    def _continue_with_tool_results(
        self, 
        context: WorkflowContext, 
        tool_results: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Continue conversation after tool execution.
        
        Args:
            context: Workflow context
            tool_results: Results from tool execution
            
        Returns:
            Dictionary with final response
        """
        # For development - return simple response
        return {
            "content": f"Processed {len(tool_results)} tool results",
            "metadata": {
                "model": "development",
                "tool_results_processed": len(tool_results)
            }
        }
        
        # Actual implementation (commented out for development):
        # # Build messages including tool results
        # messages = self._build_conversation_history(context)
        # 
        # # Add tool results
        # for result in tool_results:
        #     messages.append({
        #         "role": "tool",
        #         "tool_call_id": result["tool_call_id"],
        #         "content": json.dumps(result["result"])
        #     })
        # 
        # # Get final response
        # response = self.openai_client.chat.completions.create(
        #     model="gpt-4o-mini",
        #     messages=messages
        # )
        # 
        # message = response.choices[0].message
        # 
        # return {
        #     "content": message.content or "",
        #     "metadata": {
        #         "model": "gpt-4o-mini",
        #         "tool_results_processed": len(tool_results),
        #         "usage": response.usage.model_dump() if response.usage else {}
        #     }
        # }
    
    def approve_tool_execution(
        self, 
        chat: UserChat, 
        message: ChatMessage, 
        approved_tools: List[str]
    ) -> ChatMessage:
        """
        Execute approved tools and continue the conversation.
        
        Args:
            chat: The user chat
            message: The message with pending tool approvals
            approved_tools: List of approved tool call IDs
            
        Returns:
            The assistant's response after tool execution
        """
        agent_context = message.get_agent_context()
        pending_approvals = agent_context.get("pending_approvals", [])
        
        # Execute approved tools
        tool_results = []
        for approval in pending_approvals:
            if approval["tool_call_id"] in approved_tools:
                try:
                    result = tool_registry.execute_tool(
                        name=approval["tool_name"],
                        user=chat.user,
                        arguments=approval["arguments"],
                        approved=True
                    )
                    
                    tool_results.append({
                        "tool_call_id": approval["tool_call_id"],
                        "result": result
                    })
                    
                    # Create tool response message
                    chat.add_message(
                        message_type=MessageType.TOOL_RESPONSE,
                        content=json.dumps(result),
                        role=MessageRole.TOOL,
                        metadata={
                            "tool_name": approval["tool_name"],
                            "tool_call_id": approval["tool_call_id"],
                            "arguments": approval["arguments"],
                            "approved": True
                        }
                    )
                    
                except Exception as e:
                    error_result = {"error": str(e)}
                    tool_results.append({
                        "tool_call_id": approval["tool_call_id"],
                        "result": error_result
                    })
        
        # Continue conversation with approved tool results
        context = WorkflowContext(
            user=chat.user,
            chat=chat,
            workflow=chat.workflow,
            current_message=message
        )
        
        response = self._continue_with_tool_results(context, tool_results)
        
        # Create final assistant message
        assistant_message = chat.add_message(
            message_type=MessageType.ASSISTANT_MESSAGE,
            content=response["content"],
            role=MessageRole.ASSISTANT,
            metadata=response.get("metadata", {})
        )
        
        return assistant_message


# Global workflow engine instance
workflow_engine = WorkflowEngine()