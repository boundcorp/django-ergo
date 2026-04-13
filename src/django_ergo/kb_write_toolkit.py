"""KBWriteToolkit — scoped write tools for a single knowledgebase.

Also provides module-level create_article(), update_article(), delete_article()
functions used by both KBWriteToolkit and KBSuggestToolkit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django_ergo.conversation.toolkit import Toolkit

if TYPE_CHECKING:
    from django_ergo.conversation.adapters import ToolAdapter
    from django_ergo.models import Knowledgebase


def _next_hex_code(existing_codes: set[str]) -> str:
    """Find the next available single-char hex code (0-9, A-F, then 10+)."""
    for i in range(256):
        code = format(i, "X")
        if code not in existing_codes:
            return code
    msg = "No available hierarchy codes"
    raise ValueError(msg)


def _next_child_code(parent_code: str, existing_codes: set[str]) -> str:
    """Find the next available child code under a parent."""
    for i in range(256):
        suffix = format(i, "X")
        code = f"{parent_code}{suffix}"
        if code not in existing_codes:
            return code
    msg = f"No available child codes under '{parent_code}'"
    raise ValueError(msg)


# ---------------------------------------------------------------------------
# Shared write functions — used by KBWriteToolkit and KBSuggestToolkit
# ---------------------------------------------------------------------------


def create_article(  # noqa: PLR0913
    kb: Knowledgebase,
    title: str,
    content: str,
    hierarchy_code: str | None = None,
    parent_code: str | None = None,
    section: str | None = None,
    summary: str | None = None,
) -> str:
    """Create an article in the KB. Returns confirmation string."""
    from django_ergo.models import Article

    placement_params = sum(1 for p in (hierarchy_code, parent_code, section) if p)
    if placement_params > 1:
        msg = "Provide only one of hierarchy_code, parent_code, or section"
        raise ValueError(msg)

    existing_codes = set(kb.articles.values_list("hierarchy_code", flat=True))

    if hierarchy_code:
        if hierarchy_code in existing_codes:
            msg = f"Article with code '{hierarchy_code}' already exists in '{kb.name}'"
            raise ValueError(msg)
    elif parent_code:
        child_codes = {
            c
            for c in existing_codes
            if c.startswith(parent_code) and len(c) == len(parent_code) + 1
        }
        hierarchy_code = _next_child_code(parent_code, child_codes)
    elif section:
        child_codes = {
            c
            for c in existing_codes
            if c.startswith(section) and len(c) == len(section) + 1
        }
        hierarchy_code = _next_child_code(section, child_codes)
    else:
        msg = (
            "Provide one of: hierarchy_code, parent_code, or section. "
            "Use section (e.g. '1' for Characters) to auto-assign a code "
            "within that wiki section."
        )
        raise ValueError(msg)

    create_kwargs: dict = {
        "knowledgebase": kb,
        "title": title,
        "content": content,
        "hierarchy_code": hierarchy_code,
    }
    if summary:
        create_kwargs["summary"] = summary

    Article.objects.create(**create_kwargs)
    return f'Created article {hierarchy_code}: "{title}" in {kb.name}'


def update_article(
    kb: Knowledgebase,
    hierarchy_code: str,
    title: str | None = None,
    content: str | None = None,
    summary: str | None = None,
) -> str:
    """Update an existing article. Returns confirmation string."""
    updatable = {}
    if title is not None:
        updatable["title"] = title
    if content is not None:
        updatable["content"] = content
    if summary is not None:
        updatable["summary"] = summary

    if not updatable:
        msg = "No fields to update. Provide at least one of: title, content, summary"
        raise ValueError(msg)

    try:
        article = kb.articles.get(hierarchy_code=hierarchy_code)
    except kb.articles.model.DoesNotExist:
        msg = f"Article '{hierarchy_code}' not found in '{kb.name}'"
        raise ValueError(msg) from None

    for field, value in updatable.items():
        setattr(article, field, value)
    article.save(update_fields=list(updatable.keys()))

    fields_str = ", ".join(updatable.keys())
    return f"Updated article {hierarchy_code} in {kb.name}: {fields_str}"


def delete_article(kb: Knowledgebase, hierarchy_code: str) -> str:
    """Delete an article. Returns confirmation string."""
    try:
        article = kb.articles.get(hierarchy_code=hierarchy_code)
    except kb.articles.model.DoesNotExist:
        msg = f"Article '{hierarchy_code}' not found in '{kb.name}'"
        raise ValueError(msg) from None

    title = article.title
    article.delete()
    return f'Deleted article {hierarchy_code}: "{title}" from {kb.name}'


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

KB_WRITE_TOOLS = [
    {
        "name": "kb_create_article",
        "description": "Create a new article in the knowledge base. Provide exactly one of: section, parent_code, or hierarchy_code.",
        "parameters": {
            "title": {
                "type": "string",
                "required": True,
                "description": "Article title",
            },
            "content": {
                "type": "string",
                "required": True,
                "description": "Article content",
            },
            "section": {
                "type": "string",
                "required": False,
                "description": "Wiki section prefix (e.g. '1' for Characters, '2' for Settings). Auto-generates the next available code in that section.",
            },
            "hierarchy_code": {
                "type": "string",
                "required": False,
                "description": "Explicit hierarchy code for placement. Conflicts if already taken.",
            },
            "parent_code": {
                "type": "string",
                "required": False,
                "description": "Place as child of this article. Auto-generates sub-code.",
            },
            "summary": {
                "type": "string",
                "required": False,
                "description": "Article summary",
            },
        },
    },
    {
        "name": "kb_update_article",
        "description": "Update an existing article's title, content, or summary",
        "parameters": {
            "hierarchy_code": {
                "type": "string",
                "required": True,
                "description": "Hierarchy code of the article to update",
            },
            "title": {
                "type": "string",
                "required": False,
                "description": "New title",
            },
            "content": {
                "type": "string",
                "required": False,
                "description": "New content",
            },
            "summary": {
                "type": "string",
                "required": False,
                "description": "New summary",
            },
        },
    },
    {
        "name": "kb_delete_article",
        "description": "Delete an article from the knowledge base",
        "parameters": {
            "hierarchy_code": {
                "type": "string",
                "required": True,
                "description": "Hierarchy code of the article to delete",
            },
        },
    },
]

KB_WRITE_TOOL_NAMES = {t["name"] for t in KB_WRITE_TOOLS}


class KBWriteToolkit(Toolkit):
    """Scoped toolkit for writing articles to a single knowledgebase."""

    def __init__(self, knowledgebase: Knowledgebase):
        self.knowledgebase = knowledgebase

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in KB_WRITE_TOOL_NAMES

    def get_tools_schema(self, adapter: ToolAdapter) -> list[dict]:
        from django_ergo.tools import ToolConfig

        schemas = []
        for tool_def in KB_WRITE_TOOLS:
            config = ToolConfig(
                name=tool_def["name"],
                description=tool_def["description"],
                parameters=tool_def["parameters"],
                requires_approval=True,
                readonly=False,
            )
            schemas.append(adapter.to_engine_schema(config))
        return schemas

    def execute_tool(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "kb_create_article":
            return create_article(
                self.knowledgebase,
                title=arguments["title"],
                content=arguments["content"],
                hierarchy_code=arguments.get("hierarchy_code"),
                parent_code=arguments.get("parent_code"),
                section=arguments.get("section"),
                summary=arguments.get("summary"),
            )
        if tool_name == "kb_update_article":
            return update_article(
                self.knowledgebase,
                hierarchy_code=arguments["hierarchy_code"],
                title=arguments.get("title"),
                content=arguments.get("content"),
                summary=arguments.get("summary"),
            )
        if tool_name == "kb_delete_article":
            return delete_article(
                self.knowledgebase,
                hierarchy_code=arguments["hierarchy_code"],
            )
        msg = f"Unknown tool: {tool_name}"
        raise ValueError(msg)

    def get_bound_knowledgebases(self) -> list[tuple]:
        return [(self.knowledgebase, "write")]

    def render_overview(self) -> str:
        kb = self.knowledgebase
        article_count = kb.articles.count()
        overview = (
            f"=== KB Write Access: {kb.name} (kb_id: {kb.id}) ===\n"
            f"Articles: {article_count}\n"
            f"Available tools: kb_create_article, kb_update_article, kb_delete_article\n"
            f"Note: All write operations require approval."
        )
        if kb.organization_strategy:
            overview = (
                f"## KB Organization Strategy\n{kb.organization_strategy}\n\n"
                + overview
            )
        return overview
