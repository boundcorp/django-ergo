"""
Knowledge base tools for Django Ergo.

Provides tools for searching and managing knowledge bases that can be used
by AI agents in workflows.
"""

from typing import List, Dict, Any, Optional
from django.contrib.auth import get_user_model
from django_ergo.models import Knowledgebase, Article
from django_ergo.tools import tool
from django_ergo.tools import tool_registry

User = get_user_model()


@tool(
    name="search_user_kb",
    description="Search articles in user's knowledge bases using hybrid semantic and text search",
    readonly=True
)
def search_user_kb(
    user: User,
    query: str,
    kb_name: Optional[str] = None,
    top_k: int = 5
) -> List[Dict[str, Any]]:
    """
    Search articles in user's knowledge bases.
    
    Args:
        user: The user whose knowledge bases to search
        query: Search query
        kb_name: Optional specific knowledge base name to search
        top_k: Number of results to return
        
    Returns:
        List of matching articles with metadata
    """
    # Get user's knowledge bases
    kb_filter = {"owner_id": str(user.id)}
    if kb_name:
        kb_filter["name__icontains"] = kb_name
    
    kbs = Knowledgebase.objects.filter(**kb_filter)
    
    if not kbs.exists():
        return []
    
    # Search across all user's knowledge bases
    results = []
    for kb in kbs:
        # Use Article's semantic search capability if available
        try:
            articles = Article.objects.filter(knowledgebase=kb).semantic_search_content(query)[:top_k]
        except AttributeError:
            # Fallback to simple text search if semantic search not available
            articles = Article.objects.filter(
                knowledgebase=kb,
                content__icontains=query
            )[:top_k]
        
        for article in articles:
            results.append({
                "id": str(article.id),
                "title": article.title,
                "content": article.content[:500] + "..." if len(article.content) > 500 else article.content,
                "summary": article.summary or "",
                "hierarchy_code": article.hierarchy_code,
                "knowledgebase": kb.name,
                "knowledgebase_id": str(kb.id),
            })
    
    return results[:top_k]


@tool(
    name="search_garden_kb",
    description="Search the garden knowledgebase for articles about plants, gardening, and related topics",
    readonly=True
)
def search_garden_kb(
    user: User,
    query: str,
    top_k: int = 5
) -> List[Dict[str, Any]]:
    """
    Search articles in garden knowledge bases.
    
    Args:
        user: The user making the search request
        query: Search query about gardening topics
        top_k: Number of results to return
        
    Returns:
        List of matching garden articles
    """
    # Look for garden-related knowledge bases
    garden_kbs = Knowledgebase.objects.filter(
        name__icontains="garden"
    )
    
    if not garden_kbs.exists():
        return []
    
    results = []
    for kb in garden_kbs:
        # Use semantic search if available
        try:
            articles = Article.objects.filter(knowledgebase=kb).semantic_search_content(query)[:top_k]
        except AttributeError:
            # Fallback to text search
            articles = Article.objects.filter(
                knowledgebase=kb,
                content__icontains=query
            )[:top_k]
        
        for article in articles:
            results.append({
                "id": str(article.id),
                "title": article.title,
                "content": article.content[:500] + "..." if len(article.content) > 500 else article.content,
                "summary": article.summary or "",
                "hierarchy_code": article.hierarchy_code,
                "knowledgebase": kb.name,
                "knowledgebase_id": str(kb.id),
            })
    
    return results[:top_k]


@tool(
    name="get_kb_table_of_contents",
    description="Get the table of contents for a specific knowledge base",
    readonly=True
)
def get_kb_table_of_contents(
    user: User,
    kb_name: str
) -> Dict[str, Any]:
    """
    Get the table of contents for a knowledge base.
    
    Args:
        user: The user requesting the table of contents
        kb_name: Name of the knowledge base
        
    Returns:
        Dictionary with table of contents information
    """
    try:
        kb = Knowledgebase.objects.get(
            name__iexact=kb_name,
            owner_id=str(user.id)
        )
    except Knowledgebase.DoesNotExist:
        return {"error": f"Knowledge base '{kb_name}' not found"}
    
    # Get all articles ordered by hierarchy
    articles = kb.articles.order_by('hierarchy_code')
    
    toc_entries = []
    for article in articles:
        toc_entries.append({
            "hierarchy_code": article.hierarchy_code,
            "title": article.title,
            "summary": article.summary or "",
            "id": str(article.id),
        })
    
    return {
        "knowledgebase": kb.name,
        "description": kb.description,
        "article_count": len(toc_entries),
        "table_of_contents": toc_entries
    }


@tool(
    name="get_article_by_hierarchy",
    description="Get a specific article by its hierarchy code",
    readonly=True
)
def get_article_by_hierarchy(
    user: User,
    kb_name: str,
    hierarchy_code: str
) -> Dict[str, Any]:
    """
    Get a specific article by its hierarchy code.
    
    Args:
        user: The user requesting the article
        kb_name: Name of the knowledge base
        hierarchy_code: Hierarchy code of the article
        
    Returns:
        Dictionary with article information
    """
    try:
        kb = Knowledgebase.objects.get(
            name__iexact=kb_name,
            owner_id=str(user.id)
        )
        article = kb.articles.get(hierarchy_code=hierarchy_code)
    except (Knowledgebase.DoesNotExist, Article.DoesNotExist):
        return {"error": f"Article '{hierarchy_code}' not found in knowledge base '{kb_name}'"}
    
    return {
        "id": str(article.id),
        "title": article.title,
        "content": article.content,
        "summary": article.summary or "",
        "hierarchy_code": article.hierarchy_code,
        "knowledgebase": kb.name,
        "knowledgebase_id": str(kb.id),
    }


@tool(
    name="list_user_knowledgebases",
    description="List all knowledge bases owned by the user",
    readonly=True
)
def list_user_knowledgebases(user: User) -> List[Dict[str, Any]]:
    """
    List all knowledge bases owned by the user.
    
    Args:
        user: The user whose knowledge bases to list
        
    Returns:
        List of knowledge base information
    """
    kbs = Knowledgebase.objects.filter(owner_id=str(user.id))
    
    results = []
    for kb in kbs:
        article_count = kb.articles.count()
        results.append({
            "id": str(kb.id),
            "name": kb.name,
            "description": kb.description,
            "article_count": article_count,
            "created_at": kb.created_at.isoformat(),
            "updated_at": kb.updated_at.isoformat(),
        })
    
    return results


@tool(
    name="create_article",
    description="Create a new article in a knowledge base",
    requires_approval=True
)
def create_article(
    user: User,
    kb_name: str,
    title: str,
    content: str,
    hierarchy_code: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create a new article in a knowledge base.
    
    Args:
        user: The user creating the article
        kb_name: Name of the knowledge base
        title: Article title
        content: Article content
        hierarchy_code: Optional hierarchy code (auto-generated if not provided)
        
    Returns:
        Dictionary with created article information
    """
    try:
        kb = Knowledgebase.objects.get(
            name__iexact=kb_name,
            owner_id=str(user.id)
        )
    except Knowledgebase.DoesNotExist:
        return {"error": f"Knowledge base '{kb_name}' not found"}
    
    # Auto-generate hierarchy code if not provided
    if not hierarchy_code:
        # Find the next available top-level hierarchy code
        existing_codes = set(
            kb.articles.filter(hierarchy_code__regex=r'^.$')
            .values_list('hierarchy_code', flat=True)
        )
        
        # Generate next available hex code (0-F)
        for i in range(16):
            code = format(i, 'X')
            if code not in existing_codes:
                hierarchy_code = code
                break
        
        if not hierarchy_code:
            return {"error": "No available hierarchy codes at top level"}
    
    # Check if hierarchy code already exists
    if kb.articles.filter(hierarchy_code=hierarchy_code).exists():
        return {"error": f"Article with hierarchy code '{hierarchy_code}' already exists"}
    
    # Create the article
    article = Article.objects.create(
        knowledgebase=kb,
        title=title,
        content=content,
        hierarchy_code=hierarchy_code
    )
    
    return {
        "id": str(article.id),
        "title": article.title,
        "hierarchy_code": article.hierarchy_code,
        "knowledgebase": kb.name,
        "message": "Article created successfully"
    }


@tool(
    name="update_article",
    description="Update an existing article in a knowledge base",
    requires_approval=True
)
def update_article(
    user: User,
    kb_name: str,
    hierarchy_code: str,
    title: Optional[str] = None,
    content: Optional[str] = None,
    append_content: Optional[str] = None
) -> Dict[str, Any]:
    """
    Update an existing article in a knowledge base.
    
    Args:
        user: The user updating the article
        kb_name: Name of the knowledge base
        hierarchy_code: Hierarchy code of the article to update
        title: New title (optional)
        content: New content (optional, replaces existing content)
        append_content: Content to append to existing content (optional)
        
    Returns:
        Dictionary with updated article information
    """
    try:
        kb = Knowledgebase.objects.get(
            name__iexact=kb_name,
            owner_id=str(user.id)
        )
        article = kb.articles.get(hierarchy_code=hierarchy_code)
    except (Knowledgebase.DoesNotExist, Article.DoesNotExist):
        return {"error": f"Article '{hierarchy_code}' not found in knowledge base '{kb_name}'"}
    
    # Update fields if provided
    updated_fields = []
    
    if title is not None:
        article.title = title
        updated_fields.append("title")
    
    if content is not None:
        article.content = content
        updated_fields.append("content")
    elif append_content is not None:
        article.content = article.content + "\n\n" + append_content
        updated_fields.append("content (appended)")
    
    if not updated_fields:
        return {"error": "No updates provided. Specify title, content, or append_content."}
    
    article.save()
    
    return {
        "id": str(article.id),
        "title": article.title,
        "hierarchy_code": article.hierarchy_code,
        "knowledgebase": kb.name,
        "updated_fields": updated_fields,
        "message": f"Article updated successfully. Updated: {', '.join(updated_fields)}"
    }


@tool_registry.register_tool(
    name="delete_user_article",
    description="Delete an article from the user's knowledge base. This action is destructive and requires approval.",
    requires_approval=True,  # This tool requires approval
    readonly=False
)
def delete_user_article(user, article_id: str):
    """
    Delete an article from the user's knowledge base.
    This tool requires approval before execution.
    
    Args:
        user: The user making the request
        article_id: ID of the article to delete
        
    Returns:
        dict: Deletion confirmation with details
    """
    from django_ergo.models import Article
    
    try:
        # Find the article
        article = Article.objects.get(id=article_id, knowledgebase__owner_id=str(user.id))
        article_title = article.title
        article_kb = article.knowledgebase.name
        
        # Delete the article
        article.delete()
        
        return {
            "success": True,
            "message": f"Successfully deleted article '{article_title}' from knowledge base '{article_kb}'",
            "deleted_article": {
                "id": article_id,
                "title": article_title,
                "knowledgebase": article_kb
            }
        }
        
    except Article.DoesNotExist:
        return {
            "success": False,
            "error": f"Article with ID '{article_id}' not found or you don't have permission to delete it"
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to delete article: {str(e)}"
        }


@tool_registry.register_tool(
    name="send_email_notification",
    description="Send an email notification to external recipients. Requires approval for security.",
    requires_approval=True,  # This tool requires approval
    readonly=False
)
def send_email_notification(user, recipient: str, subject: str, message: str):
    """
    Send an email notification to external recipients.
    This tool requires approval before execution for security reasons.
    
    Args:
        user: The user making the request
        recipient: Email address of the recipient
        subject: Email subject line
        message: Email message content
        
    Returns:
        dict: Email sending status
    """
    # Note: This is a demonstration tool - in production you'd integrate with actual email service
    return {
        "success": True,
        "message": f"Email notification sent successfully",
        "email_details": {
            "recipient": recipient,
            "subject": subject,
            "message": message,
            "sender": user.email if hasattr(user, 'email') else 'system@example.com',
            "status": "simulated_sent"
        }
    }