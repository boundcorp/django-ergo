"""
Ingestion workflows for learning from various content sources.
"""
import logging
import re
from typing import Dict, Any, Optional, List
from django.contrib.auth.models import User
from django_ergo.models import Workflow, Knowledgebase, UserChat, ChatMessage
from django_ergo.workflow_engine import BaseWorkflowEngine

logger = logging.getLogger(__name__)


class ChatHistoryIngestionWorkflow(BaseWorkflowEngine):
    """Workflow for ingesting chat history to learn facts and corrections."""
    
    name = "chat_history_ingestion"
    description = "Learn facts and corrections from user chat history"
    
    # Patterns that indicate corrections or facts
    CORRECTION_PATTERNS = [
        r"actually[,\s]+(.+)",
        r"no[,\s]+(?:it's|its|it is)\s+(.+)",
        r"(?:that's|thats)\s+(?:not right|wrong|incorrect)[,\s]+(.+)",
        r"correction:\s*(.+)",
        r"(?:the correct|the right)\s+(?:answer|information)\s+is\s+(.+)",
        r"(?:you're|youre|you are)\s+wrong[,\s]+(.+)",
        r"(?:that's|thats)\s+outdated[,\s]+(.+)",
        r"(?:we changed|it changed|now it's|now its)\s+(.+)",
        r"(?:my|our)\s+(.+)\s+(?:is|are)\s+(.+)",  # "my shop is in EST"
    ]
    
    FACT_PATTERNS = [
        r"(?:my|our)\s+(.+?)\s+(?:is|are)\s+(.+)",  # "my shop is in EST"
        r"(?:the|this)\s+(.+?)\s+(?:is|are)\s+(.+)",  # "the timezone is EST"
        r"(?:we|i)\s+(?:use|have|operate in)\s+(.+)",  # "we operate in EST"
    ]
    
    def get_system_prompt(self) -> str:
        """Get the system prompt for chat history ingestion."""
        return """You are a knowledge extraction assistant. Your job is to analyze chat conversations and extract important facts and corrections to update the knowledge base.

Look for:
1. User corrections to assistant responses
2. Important facts about the business (timezone, policies, procedures)
3. Context that would help future queries

When you find corrections or facts, use the update_article tool to create or update knowledge base articles.

For corrections:
- Create articles titled like "Business Context - [Topic]"
- Include both the original incorrect information and the correction
- Note when the correction was made

For facts:
- Create articles about specific business aspects
- Use clear titles like "Shop Configuration - Timezone" or "Business Hours"
- Include detailed context

Be thorough but focused on actionable information that would help answer future queries correctly."""
    
    def get_available_tools(self) -> list:
        """Return tools available for this workflow."""
        return [
            'create_article',
            'update_article',
            'search_user_kb',
            'get_kb_table_of_contents'
        ]
    
    def process(self, user: User, prompt: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Process chat history ingestion request."""
        if context is None:
            context = {}
        
        # Extract parameters from prompt
        kb_name = context.get('kb_name', 'Shop Wiki')
        topic = context.get('topic', 'general business information')
        chat_ids = context.get('chat_ids', [])
        
        # Get or create the knowledge base
        kb, _ = Knowledgebase.objects.get_or_create(
            name=kb_name,
            defaults={
                'description': f'Knowledge base for {topic}',
                'owner': user
            }
        )
        context['knowledgebase_id'] = kb.id
        
        # Prepare chat history for analysis
        if chat_ids:
            chats = UserChat.objects.filter(id__in=chat_ids, user=user)
        else:
            chats = UserChat.objects.filter(user=user)
        
        chat_content = self._format_chat_history(chats)
        
        # Create the full prompt for the AI
        full_prompt = f"""Analyze the following chat history and extract facts about {topic}:

{chat_content}

Focus on corrections, business context, and factual information that would help answer future queries about {topic}.

Create or update knowledge base articles with this information."""
        
        return super().process(user, full_prompt, context)
    
    def _format_chat_history(self, chats: List[UserChat]) -> str:
        """Format chat history for analysis."""
        formatted_content = []
        
        for chat in chats:
            formatted_content.append(f"=== Chat: {chat.title} ===")
            messages = chat.messages.order_by('created_at')
            
            for msg in messages:
                role_label = msg.role.upper()
                formatted_content.append(f"{role_label}: {msg.content}")
            
            formatted_content.append("")  # Empty line between chats
        
        return "\n".join(formatted_content)


class DocumentIngestionWorkflow(BaseWorkflowEngine):
    """Workflow for ingesting documents/PDFs/transcripts to learn about specific topics."""
    
    name = "document_ingestion"
    description = "Learn from documents, PDFs, and transcripts about specific topics"
    
    def get_system_prompt(self) -> str:
        """Get the system prompt for document ingestion."""
        return """You are a document analysis assistant. Your job is to extract relevant information from documents and create knowledge base articles.

When analyzing documents:
1. Break down complex information into focused articles
2. Use clear, descriptive titles
3. Extract key facts, procedures, and policies
4. Create hierarchical articles for complex topics
5. Include relevant quotes or references where helpful

Create multiple articles if the document covers several distinct topics. Each article should be focused on a specific aspect of the subject matter.

Use the create_article tool to add new knowledge to the knowledge base."""
    
    def get_available_tools(self) -> list:
        """Return tools available for this workflow."""
        return [
            'create_article',
            'update_article',
            'search_user_kb',
            'get_kb_table_of_contents'
        ]
    
    def process(self, user: User, prompt: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Process document ingestion request."""
        if context is None:
            context = {}
        
        # Extract parameters
        kb_name = context.get('kb_name', 'Document Knowledge Base')
        topic = context.get('topic', 'general information')
        document_content = context.get('document_content', '')
        
        # Get or create the knowledge base
        kb, _ = Knowledgebase.objects.get_or_create(
            name=kb_name,
            defaults={
                'description': f'Knowledge extracted from documents about {topic}',
                'owner': user
            }
        )
        context['knowledgebase_id'] = kb.id
        
        # Create the full prompt
        full_prompt = f"""Analyze the following document content and extract information about {topic}:

=== DOCUMENT CONTENT ===
{document_content}

=== INSTRUCTIONS ===
{prompt}

Create focused knowledge base articles covering the key information from this document."""
        
        return super().process(user, full_prompt, context)


class KnowledgeBaseReviewWorkflow(BaseWorkflowEngine):
    """Workflow for reviewing and improving existing knowledge base articles."""
    
    name = "kb_review"
    description = "Review and improve existing knowledge base articles"
    
    def get_system_prompt(self) -> str:
        """Get the system prompt for KB review."""
        return """You are a knowledge base review assistant. Your job is to analyze existing articles and improve them.

When reviewing articles:
1. Check for outdated information
2. Identify gaps or missing details
3. Improve clarity and organization
4. Add cross-references where helpful
5. Consolidate duplicate information
6. Update with new insights or corrections

Use the update_article tool to improve existing articles or create_article for new supplementary content.

Be thorough but conservative - only make changes that clearly improve the knowledge base."""
    
    def get_available_tools(self) -> list:
        """Return tools available for this workflow."""
        return [
            'create_article',
            'update_article',
            'search_user_kb',
            'get_kb_table_of_contents',
            'get_article_by_hierarchy'
        ]
    
    def process(self, user: User, prompt: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Process KB review request."""
        if context is None:
            context = {}
        
        # Extract parameters
        kb_name = context.get('kb_name', 'Shop Wiki')
        focus_area = context.get('focus_area', 'general review')
        
        # Get the knowledge base
        try:
            kb = Knowledgebase.objects.get(name=kb_name, owner=user)
        except Knowledgebase.DoesNotExist:
            return {"error": f"Knowledge base '{kb_name}' not found"}
        
        context['knowledgebase_id'] = kb.id
        
        # Create the full prompt
        full_prompt = f"""Review the knowledge base '{kb_name}' focusing on {focus_area}.

Instructions: {prompt}

First, get the table of contents to understand the current structure, then review relevant articles and suggest improvements."""
        
        return super().process(user, full_prompt, context)


def run_chat_history_ingestion(
    user: User,
    kb_name: str = "Shop Wiki",
    topic: str = "business context",
    chat_ids: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Run chat history ingestion workflow.
    
    Args:
        user: User performing the ingestion
        kb_name: Name of the knowledge base to update
        topic: Topic to focus on when extracting information
        chat_ids: Optional list of specific chat IDs to analyze
    
    Returns:
        Dictionary with ingestion results
    """
    workflow = ChatHistoryIngestionWorkflow()
    
    context = {
        'kb_name': kb_name,
        'topic': topic,
        'chat_ids': chat_ids or []
    }
    
    prompt = f"Extract and learn facts about {topic} from the chat history"
    
    return workflow.process(user, prompt, context)


def run_document_ingestion(
    user: User,
    document_content: str,
    topic: str,
    kb_name: str = "Document Knowledge Base",
    instructions: str = "Extract key information and create knowledge base articles"
) -> Dict[str, Any]:
    """
    Run document ingestion workflow.
    
    Args:
        user: User performing the ingestion
        document_content: The content of the document to analyze
        topic: Topic/subject of the document
        kb_name: Name of the knowledge base to update
        instructions: Specific instructions for what to extract
    
    Returns:
        Dictionary with ingestion results
    """
    workflow = DocumentIngestionWorkflow()
    
    context = {
        'kb_name': kb_name,
        'topic': topic,
        'document_content': document_content
    }
    
    return workflow.process(user, instructions, context)


def run_kb_review(
    user: User,
    kb_name: str = "Shop Wiki",
    focus_area: str = "general review",
    instructions: str = "Review articles for accuracy and completeness"
) -> Dict[str, Any]:
    """
    Run knowledge base review workflow.
    
    Args:
        user: User performing the review
        kb_name: Name of the knowledge base to review
        focus_area: Specific area to focus the review on
        instructions: Specific instructions for the review
    
    Returns:
        Dictionary with review results
    """
    workflow = KnowledgeBaseReviewWorkflow()
    
    context = {
        'kb_name': kb_name,
        'focus_area': focus_area
    }
    
    return workflow.process(user, instructions, context)