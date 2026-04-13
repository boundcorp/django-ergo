"""KBToolkit — scoped read-only tools for agent-driven KB access."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django_ergo.conversation.toolkit import Toolkit

if TYPE_CHECKING:
    from django_ergo.conversation.adapters import ToolAdapter
    from django_ergo.models import Knowledgebase

CONTENT_PREVIEW_MAX = 200
DEFAULT_TOP_K = 5
TOC_INDENT = "  "

KB_TOOLS = [
    {
        "name": "kb_list",
        "description": "List all available knowledge bases with descriptions and article counts",
        "parameters": {},
    },
    {
        "name": "kb_search",
        "description": "Semantic search across knowledge bases for relevant articles",
        "parameters": {
            "query": {
                "type": "string",
                "required": True,
                "description": "Search query text",
            },
            "kb_name": {
                "type": "string",
                "required": False,
                "description": "Filter to a specific KB by name",
            },
            "top_k": {
                "type": "integer",
                "required": False,
                "description": "Number of results to return (default: 5)",
            },
        },
    },
    {
        "name": "kb_get_article",
        "description": "Get the full content of a specific article by KB name and hierarchy code",
        "parameters": {
            "kb_name": {
                "type": "string",
                "required": True,
                "description": "Name of the knowledge base",
            },
            "hierarchy_code": {
                "type": "string",
                "required": True,
                "description": "Article hierarchy code (e.g., '0', '1A', '2B3')",
            },
        },
    },
    {
        "name": "kb_table_of_contents",
        "description": "Get the full table of contents for a knowledge base",
        "parameters": {
            "kb_name": {
                "type": "string",
                "required": True,
                "description": "Name of the knowledge base",
            },
        },
    },
]

KB_TOOL_NAMES = {t["name"] for t in KB_TOOLS}


class KBToolkit(Toolkit):
    """Scoped toolkit for agent-driven KB search and browsing."""

    def __init__(self, knowledgebases: list[Knowledgebase]):
        self.knowledgebases = {str(kb.id): kb for kb in knowledgebases}
        self._name_to_id: dict[str, str] = {
            kb.name: str(kb.id) for kb in knowledgebases
        }

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in KB_TOOL_NAMES

    def get_tools_schema(self, adapter: ToolAdapter) -> list[dict]:
        from django_ergo.tools import ToolConfig

        schemas = []
        for tool_def in KB_TOOLS:
            config = ToolConfig(
                name=tool_def["name"],
                description=tool_def["description"],
                parameters=tool_def["parameters"],
                readonly=True,
            )
            schemas.append(adapter.to_engine_schema(config))
        return schemas

    def execute_tool(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "kb_list":
            return self._kb_list()
        if tool_name == "kb_search":
            return self._kb_search(arguments)
        if tool_name == "kb_get_article":
            return self._kb_get_article(arguments)
        if tool_name == "kb_table_of_contents":
            return self._kb_table_of_contents(arguments)
        msg = f"Unknown tool: {tool_name}"
        raise ValueError(msg)

    def get_bound_knowledgebases(self) -> list[tuple]:
        return [(kb, "read") for kb in self.knowledgebases.values()]

    def render_overview(self) -> str:
        parts = []
        for kb_id, kb in self.knowledgebases.items():
            article_count = kb.articles.count()
            header = f"=== Knowledge Base: {kb.name} (kb_id: {kb_id}) ==="
            desc = f"Description: {kb.description}"
            count = f"Articles: {article_count}"

            top_level = kb.articles.filter(hierarchy_code__regex=r"^.$")
            toc_lines = [f"  {a.hierarchy_code}: {a.title}" for a in top_level]
            toc = (
                "Top-level sections:\n" + "\n".join(toc_lines)
                if toc_lines
                else "No articles yet."
            )

            kb_part = f"{header}\n{desc}\n{count}\n\n{toc}"
            if kb.organization_strategy:
                kb_part = (
                    f"## KB Organization Strategy\n{kb.organization_strategy}\n\n"
                    + kb_part
                )
            parts.append(kb_part)
        return "\n\n".join(parts)

    def _get_kb_by_name(self, name: str) -> Knowledgebase:
        kb_id = self._name_to_id.get(name)
        if kb_id is None:
            msg = f"Knowledge base '{name}' not found in this toolkit"
            raise ValueError(msg)
        return self.knowledgebases[kb_id]

    def _kb_list(self) -> str:
        lines = []
        for i, (kb_id, kb) in enumerate(self.knowledgebases.items(), 1):
            article_count = kb.articles.count()
            lines.append(f"{i}. {kb.name} (kb_id: {kb_id}) — {article_count} articles")
            lines.append(f"   {kb.description}")
        return "\n".join(lines)

    def _kb_search(self, args: dict) -> str:
        from django_ergo.models import Article

        query = args["query"]
        top_k = args.get("top_k", DEFAULT_TOP_K)

        if kb_name := args.get("kb_name"):
            kb = self._get_kb_by_name(kb_name)
            qs = Article.objects.filter(knowledgebase=kb)
        else:
            kb_ids = list(self.knowledgebases.keys())
            qs = Article.objects.filter(knowledgebase_id__in=kb_ids)

        try:
            results = qs.multi_field_semantic_search(query, top_k=top_k)
        except Exception as e:  # noqa: BLE001
            return f"Search failed: {e}. Try using kb_get_article or kb_table_of_contents instead."

        if not results:
            return "No results found."

        lines = []
        for i, article in enumerate(results, 1):
            distance = getattr(article, "combined_distance", None)
            distance_str = (
                f" (distance: {distance:.3f})" if distance is not None else ""
            )
            preview = article.content[:CONTENT_PREVIEW_MAX]
            if len(article.content) > CONTENT_PREVIEW_MAX:
                preview += "..."
            lines.append(
                f"Result {i}{distance_str}:\n"
                f"  KB: {article.knowledgebase.name}\n"
                f"  Article: {article.hierarchy_code} — {article.title}\n"
                f"  Preview: {preview}"
            )
        return "\n\n".join(lines)

    def _kb_get_article(self, args: dict) -> str:
        kb = self._get_kb_by_name(args["kb_name"])
        hierarchy_code = args["hierarchy_code"]

        try:
            article = kb.articles.get(hierarchy_code=hierarchy_code)
        except kb.articles.model.DoesNotExist:
            msg = f"Article '{hierarchy_code}' not found in '{kb.name}'"
            raise ValueError(msg) from None

        lines = [
            f"Article: {article.hierarchy_code} — {article.title}",
            f"KB: {kb.name}",
            "",
            "Content:",
            article.content,
        ]
        if article.summary:
            lines.extend(["", "Summary:", article.summary])
        return "\n".join(lines)

    def _kb_table_of_contents(self, args: dict) -> str:
        kb = self._get_kb_by_name(args["kb_name"])
        articles = kb.articles.all().order_by("hierarchy_code")

        lines = []
        for article in articles:
            depth = len(article.hierarchy_code) - 1
            indent = TOC_INDENT * depth
            lines.append(f"{indent}{article.hierarchy_code}: {article.title}")
        return "\n".join(lines)
