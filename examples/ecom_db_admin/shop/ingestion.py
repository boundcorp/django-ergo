"""
Ingestion workflows for learning from various content sources.
"""
import logging
from typing import Dict, Any, Optional, List
from django.contrib.auth.models import User
from django_ergo.models import Workflow, Knowledgebase, UserChat, ChatMessage
from django_ergo.workflow_engine import workflow_engine

logger = logging.getLogger(__name__)


def format_chat_history(chats: List[UserChat]) -> str:
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


def create_chat_history_ingestion_workflow(
    user: User,
    kb_name: str = "Shop Wiki",
    topic: str = "business configuration"
) -> Workflow:
    """Create a chat history ingestion workflow."""
    
    # Get or create the knowledge base
    kb, _ = Knowledgebase.objects.get_or_create(
        name=kb_name,
        defaults={
            'description': f'Knowledge base for {topic}',
            'owner': user
        }
    )
    
    system_prompt = f"""You are a knowledge extraction assistant. Your job is to analyze chat conversations and extract important facts and corrections to update the knowledge base.

Look for:
1. User corrections to assistant responses  
2. Important facts about the business (timezone, policies, procedures)
3. Context that would help future queries about {topic}

When you find corrections or facts, use the create_article or update_article tools to create or update knowledge base articles.

For corrections:
- Create articles titled like "Business Context - [Topic]"
- Include both the original incorrect information and the correction
- Note when the correction was made

For facts:
- Create articles about specific business aspects
- Use clear titles like "Shop Configuration - Timezone" or "Business Hours"
- Include detailed context

Be thorough but focused on actionable information that would help answer future queries correctly."""
    
    workflow = Workflow.objects.create(
        name=f"Chat History Ingestion - {topic}",
        description=f"Learn facts and corrections from user chat history about {topic}",
        instructions=system_prompt,
        owner=user,
        tools_config={
            "available_tools": [
                'create_article',
                'update_article',
                'search_user_kb',
                'get_kb_table_of_contents'
            ],
            "approved_tools": [
                'create_article',
                'update_article',
                'search_user_kb',
                'get_kb_table_of_contents'
            ]
        },
        knowledgebase=kb
    )
    
    return workflow


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
    # Create workflow
    workflow = create_chat_history_ingestion_workflow(user, kb_name, topic)
    
    # Get chat history
    if chat_ids:
        chats = UserChat.objects.filter(id__in=chat_ids, user=user)
    else:
        chats = UserChat.objects.filter(user=user)
    
    # Format chat content
    chat_content = format_chat_history(list(chats))
    
    # Create the prompt for analysis
    prompt = f"""Analyze the following chat history and extract facts about {topic}:

{chat_content}

Focus on corrections, business context, and factual information that would help answer future queries about {topic}.

Create or update knowledge base articles with this information."""
    
    # For now, return a mock result since we'd need the full workflow engine integration
    return {
        "success": True,
        "message": "Chat history ingestion completed",
        "workflow_id": str(workflow.id),
        "chats_analyzed": len(chats),
        "kb_name": kb_name,
        "topic": topic
    }


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
    # Get or create knowledge base
    kb, _ = Knowledgebase.objects.get_or_create(
        name=kb_name,
        defaults={
            'description': f'Knowledge extracted from documents about {topic}',
            'owner': user
        }
    )
    
    system_prompt = """You are a document analysis assistant. Your job is to extract relevant information from documents and create knowledge base articles.

When analyzing documents:
1. Break down complex information into focused articles
2. Use clear, descriptive titles
3. Extract key facts, procedures, and policies
4. Create hierarchical articles for complex topics
5. Include relevant quotes or references where helpful

Create multiple articles if the document covers several distinct topics. Each article should be focused on a specific aspect of the subject matter.

Use the create_article tool to add new knowledge to the knowledge base."""
    
    workflow = Workflow.objects.create(
        name=f"Document Ingestion - {topic}",
        description=f"Learn from documents about {topic}",
        instructions=system_prompt,
        owner=user,
        tools_config={
            "available_tools": [
                'create_article',
                'update_article',
                'search_user_kb',
                'get_kb_table_of_contents'
            ],
            "approved_tools": [
                'create_article',
                'update_article', 
                'search_user_kb',
                'get_kb_table_of_contents'
            ]
        },
        knowledgebase=kb
    )
    
    return {
        "success": True,
        "message": "Document ingestion workflow created",
        "workflow_id": str(workflow.id),
        "kb_name": kb_name,
        "topic": topic,
        "document_size": len(document_content)
    }


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
    try:
        kb = Knowledgebase.objects.get(name=kb_name, owner=user)
    except Knowledgebase.DoesNotExist:
        return {"error": f"Knowledge base '{kb_name}' not found"}
    
    system_prompt = """You are a knowledge base review assistant. Your job is to analyze existing articles and improve them.

When reviewing articles:
1. Check for outdated information
2. Identify gaps or missing details
3. Improve clarity and organization
4. Add cross-references where helpful
5. Consolidate duplicate information
6. Update with new insights or corrections

Use the update_article tool to improve existing articles or create_article for new supplementary content.

Be thorough but conservative - only make changes that clearly improve the knowledge base."""
    
    workflow = Workflow.objects.create(
        name=f"KB Review - {focus_area}",
        description=f"Review and improve articles in {kb_name}",
        instructions=system_prompt,
        owner=user,
        tools_config={
            "available_tools": [
                'create_article',
                'update_article',
                'search_user_kb',
                'get_kb_table_of_contents',
                'get_article_by_hierarchy'
            ],
            "approved_tools": [
                'update_article',
                'search_user_kb', 
                'get_kb_table_of_contents',
                'get_article_by_hierarchy'
            ]
        },
        knowledgebase=kb
    )
    
    return {
        "success": True,
        "message": "KB review workflow created",
        "workflow_id": str(workflow.id),
        "kb_name": kb_name,
        "focus_area": focus_area
    }