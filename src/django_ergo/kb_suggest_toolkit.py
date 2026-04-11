"""KBSuggestToolkit — propose KB changes for later review."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django_ergo.conversation.toolkit import Toolkit
from django_ergo.kb_write_toolkit import create_article
from django_ergo.kb_write_toolkit import delete_article
from django_ergo.kb_write_toolkit import update_article

if TYPE_CHECKING:
    from django_ergo.conversation.adapters import ToolAdapter
    from django_ergo.models import Knowledgebase

KB_SUGGEST_TOOLS = [
    {
        "name": "kb_suggest_create",
        "description": "Suggest creating a new article in the knowledge base (recorded for later review). Provide exactly one of: section, parent_code, or hierarchy_code.",
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
                "description": "Explicit hierarchy code for placement",
            },
            "parent_code": {
                "type": "string",
                "required": False,
                "description": "Place as child of this article",
            },
            "summary": {
                "type": "string",
                "required": False,
                "description": "Article summary",
            },
        },
    },
    {
        "name": "kb_suggest_update",
        "description": "Suggest updating an existing article (recorded for later review)",
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
        "name": "kb_suggest_delete",
        "description": "Suggest deleting an article (recorded for later review)",
        "parameters": {
            "hierarchy_code": {
                "type": "string",
                "required": True,
                "description": "Hierarchy code of the article to delete",
            },
        },
    },
]

KB_SUGGEST_TOOL_NAMES = {t["name"] for t in KB_SUGGEST_TOOLS}


class KBSuggestToolkit(Toolkit):
    """Propose KB changes without executing them. Suggestions accumulate for review."""

    def __init__(self, knowledgebase: Knowledgebase):
        self.knowledgebase = knowledgebase
        self._suggestions: list[dict] = []

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in KB_SUGGEST_TOOL_NAMES

    def get_tools_schema(self, adapter: ToolAdapter) -> list[dict]:
        from django_ergo.tools import ToolConfig

        schemas = []
        for tool_def in KB_SUGGEST_TOOLS:
            config = ToolConfig(
                name=tool_def["name"],
                description=tool_def["description"],
                parameters=tool_def["parameters"],
                requires_approval=False,
                readonly=False,
            )
            schemas.append(adapter.to_engine_schema(config))
        return schemas

    def execute_tool(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "kb_suggest_create":
            return self._suggest_create(arguments)
        if tool_name == "kb_suggest_update":
            return self._suggest_update(arguments)
        if tool_name == "kb_suggest_delete":
            return self._suggest_delete(arguments)
        msg = f"Unknown tool: {tool_name}"
        raise ValueError(msg)

    def get_bound_knowledgebases(self) -> list[tuple]:
        return [(self.knowledgebase, "suggest")]

    def render_overview(self) -> str:
        kb = self.knowledgebase
        article_count = kb.articles.count()
        return (
            f"=== KB Suggestions: {kb.name} (kb_id: {kb.id}) ===\n"
            f"Articles: {article_count}\n"
            f"Available tools: kb_suggest_create, kb_suggest_update, kb_suggest_delete\n"
            f"Suggestions are recorded for later review — no changes are made immediately."
        )

    def get_suggestions(self) -> list[dict]:
        """Return all accumulated suggestions."""
        return list(self._suggestions)

    def clear(self) -> None:
        """Discard all suggestions."""
        self._suggestions.clear()

    def apply_suggestions(self, indices: list[int] | None = None) -> list[str]:
        """Apply suggestions, executing them against the DB.

        Args:
            indices: Specific suggestion indices to apply. If None, apply all.

        Returns:
            List of result strings (one per applied suggestion).
            Failed suggestions include the error message instead of raising.
        """
        if not self._suggestions:
            return []

        if indices is None:
            to_apply = list(enumerate(self._suggestions))
        else:
            to_apply = [(i, self._suggestions[i]) for i in indices]

        results = []
        applied_indices = set()
        for idx, suggestion in to_apply:
            try:
                result = self._execute_suggestion(suggestion)
                results.append(result)
            except ValueError as e:
                results.append(f"Error: {e}")
            applied_indices.add(idx)

        # Remove applied suggestions in reverse order to preserve indices
        for idx in sorted(applied_indices, reverse=True):
            self._suggestions.pop(idx)

        return results

    def _execute_suggestion(self, suggestion: dict) -> str:
        action = suggestion["action"]
        kb = self.knowledgebase

        if action == "create":
            return create_article(
                kb,
                title=suggestion["title"],
                content=suggestion["content"],
                hierarchy_code=suggestion.get("hierarchy_code"),
                parent_code=suggestion.get("parent_code"),
                section=suggestion.get("section"),
                summary=suggestion.get("summary"),
            )
        if action == "update":
            return update_article(
                kb,
                hierarchy_code=suggestion["hierarchy_code"],
                title=suggestion.get("title"),
                content=suggestion.get("content"),
                summary=suggestion.get("summary"),
            )
        if action == "delete":
            return delete_article(kb, hierarchy_code=suggestion["hierarchy_code"])

        msg = f"Unknown action: {action}"
        raise ValueError(msg)

    def _suggest_create(self, args: dict) -> str:
        suggestion = {
            "action": "create",
            "title": args["title"],
            "content": args["content"],
            "section": args.get("section"),
            "hierarchy_code": args.get("hierarchy_code"),
            "parent_code": args.get("parent_code"),
            "summary": args.get("summary"),
        }
        self._suggestions.append(suggestion)
        placement = ""
        if args.get("hierarchy_code"):
            placement = f" at code {args['hierarchy_code']}"
        elif args.get("parent_code"):
            placement = f" under parent {args['parent_code']}"
        elif args.get("section"):
            placement = f" in section {args['section']}"
        return f'Suggestion recorded: CREATE article "{args["title"]}"{placement}'

    def _suggest_update(self, args: dict) -> str:
        suggestion = {
            "action": "update",
            "hierarchy_code": args["hierarchy_code"],
            "title": args.get("title"),
            "content": args.get("content"),
            "summary": args.get("summary"),
        }
        self._suggestions.append(suggestion)
        fields = [k for k in ("title", "content", "summary") if args.get(k) is not None]
        fields_str = ", ".join(fields) if fields else "no fields"
        return f"Suggestion recorded: UPDATE article {args['hierarchy_code']} ({fields_str})"

    def _suggest_delete(self, args: dict) -> str:
        suggestion = {
            "action": "delete",
            "hierarchy_code": args["hierarchy_code"],
        }
        self._suggestions.append(suggestion)
        return f"Suggestion recorded: DELETE article {args['hierarchy_code']}"
