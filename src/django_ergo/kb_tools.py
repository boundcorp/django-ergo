"""
Knowledge base tools for Django Ergo.

Provides tools for searching and managing knowledge bases that can be used
by AI agents in workflows.
"""

from typing import List, Dict, Any, Optional
from django.contrib.auth import get_user_model
from django_ergo.models import Knowledgebase, Article
from django_ergo.tools import tool

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
    articles = Article.objects.filter(
        knowledgebase__in=kbs
    ).hybrid_search(query, top_k=top_k)
    
    results = []
    for article in articles:
        results.append({
            "id": str(article.id),
            "title": article.title,
            "content": article.content,
            "summary": article.summary or "",
            "hierarchy_code": article.hierarchy_code,
            "knowledgebase": article.knowledgebase.name,
            "knowledgebase_id": str(article.knowledgebase.id),
        })
    
    return results


@tool(
    name="search_garden_kb",
    description="Search articles in garden-related knowledge bases",
    readonly=True
)
def search_garden_kb(
    user: User,
    query: str,
    top_k: int = 5
) -> List[Dict[str, Any]]:
    """
    Search articles in garden-related knowledge bases.
    
    Args:
        user: The user making the search
        query: Search query
        top_k: Number of results to return
        
    Returns:
        List of matching articles from garden knowledge bases
    """
    # Search in knowledge bases with garden-related names
    garden_kbs = Knowledgebase.objects.filter(
        name__icontains="garden"
    ).union(
        Knowledgebase.objects.filter(
            name__icontains="plant"
        )
    ).union(
        Knowledgebase.objects.filter(
            description__icontains="garden"
        )
    )
    
    if not garden_kbs.exists():
        return []
    
    articles = Article.objects.filter(
        knowledgebase__in=garden_kbs
    ).hybrid_search(query, top_k=top_k)
    
    results = []
    for article in articles:
        results.append({
            "id": str(article.id),
            "title": article.title,
            "content": article.content,
            "summary": article.summary or "",
            "hierarchy_code": article.hierarchy_code,
            "knowledgebase": article.knowledgebase.name,
            "knowledgebase_id": str(article.knowledgebase.id),
        })
    
    return results


@tool(
    name="get_kb_table_of_contents",
    description="Get table of contents for a knowledge base",
    readonly=True
)
def get_kb_table_of_contents(
    user: User,
    kb_name: str
) -> Dict[str, Any]:
    """
    Get table of contents for a knowledge base.
    
    Args:
        user: The user requesting the TOC
        kb_name: Name of the knowledge base
        
    Returns:
        Dictionary with table of contents and metadata
    """
    try:
        kb = Knowledgebase.objects.get(
            name__iexact=kb_name,
            owner_id=str(user.id)
        )
    except Knowledgebase.DoesNotExist:
        return {"error": f"Knowledge base '{kb_name}' not found"}
    
    # Get top-level articles (single character hierarchy codes)
    top_level_articles = kb.articles.filter(
        hierarchy_code__regex=r'^.$'
    ).order_by('hierarchy_code')
    
    toc_entries = []
    for article in top_level_articles:
        toc_entries.append({
            "hierarchy_code": article.hierarchy_code,
            "title": article.title,
            "id": str(article.id),
        })
    
    return {
        "knowledgebase": kb.name,
        "knowledgebase_id": str(kb.id),
        "description": kb.description,
        "table_of_contents": toc_entries,
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
        Dictionary with article data
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