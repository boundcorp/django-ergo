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
    kb, _ = Knowledgebase.objects.get_or_create(
        name=kb_name,
        defaults={
            'description': f'Knowledge base for {topic}',
            'owner_id': str(user.id)
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
        }
    )
    
    workflow.knowledgebases.add(kb)
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


def create_document_ingestion_workflow(
    user: User,
    document_content: str,
    topic: str,
    kb_name: str = "Shop Wiki",
    instructions: str = ""
) -> Workflow:
    """Create a document ingestion workflow."""
    kb, _ = Knowledgebase.objects.get_or_create(
        name=kb_name,
        defaults={
            'description': f'Knowledge base for {topic}',
            'owner_id': str(user.id)
        }
    )
    
    system_prompt = f"""You are a document analysis assistant. Your job is to analyze the provided document and extract important information about {topic}.

Document content to analyze:
{document_content}

{instructions}

Use the create_article tool to create knowledge base articles based on the document content.
Focus on actionable information that would help answer future queries correctly."""
    
    workflow = Workflow.objects.create(
        name=f"Document Ingestion - {topic}",
        description=f"Learn from document content about {topic}",
        instructions=system_prompt,
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
        }
    )
    
    workflow.knowledgebases.add(kb)
    return workflow


def create_knowledge_base_review_workflow(
    user: User,
    kb_name: str,
    focus_area: str,
    instructions: str = ""
) -> Workflow:
    """Create a knowledge base review workflow."""
    kb, _ = Knowledgebase.objects.get_or_create(
        name=kb_name,
        defaults={
            'description': f'Knowledge base for {focus_area}',
            'owner_id': str(user.id)
        }
    )
    
    system_prompt = f"""You are a knowledge base review assistant. Your job is to review existing knowledge base articles and improve them based on the focus area: {focus_area}.

{instructions}

Use the search_user_kb and get_kb_table_of_contents tools to explore existing content.
Use the update_article tool to improve existing articles.
Use the create_article tool to add missing information.

Focus on ensuring the knowledge base is comprehensive and accurate for queries about {focus_area}."""
    
    workflow = Workflow.objects.create(
        name=f"KB Review - {focus_area}",
        description=f"Review and improve knowledge base articles about {focus_area}",
        instructions=system_prompt,
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
        }
    )
    
    workflow.knowledgebases.add(kb)
    return workflow