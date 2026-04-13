"""KBStrategyToolkit — tools for reading and updating a KB's organization strategy."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from django_ergo.conversation.toolkit import Toolkit

STRATEGY_PREVIEW_MAX = 200

if TYPE_CHECKING:
    from django_ergo.conversation.adapters import ToolAdapter
    from django_ergo.models import Knowledgebase


KB_STRATEGY_TOOLS = [
    {
        "name": "kb_get_strategy",
        "description": "Read the current organization strategy for this knowledge base.",
        "parameters": {},
    },
    {
        "name": "kb_update_strategy",
        "description": (
            "Replace the entire organization strategy with a new version. "
            "Use after analyzing KB contents to define or refine the hierarchy layout."
        ),
        "parameters": {
            "strategy": {
                "type": "string",
                "required": True,
                "description": "The new organization strategy text (replaces existing).",
            },
        },
    },
    {
        "name": "kb_propose_tree",
        "description": (
            "Propose a new tree in the strategy. "
            "Appends to the strategy without replacing existing trees. "
            'Example: propose_tree(prefix="10", title="Characters", '
            'description="Major and minor characters", '
            'entries=["Darrow", "Mustang", "Sevro"])'
        ),
        "parameters": {
            "prefix": {
                "type": "string",
                "required": True,
                "description": "Hex prefix for the tree (e.g. '10', '20', '30').",
            },
            "title": {
                "type": "string",
                "required": True,
                "description": "Human-readable title for this tree.",
            },
            "description": {
                "type": "string",
                "required": True,
                "description": "What this tree contains.",
            },
            "entries": {
                "type": "array",
                "items": {"type": "string"},
                "required": False,
                "description": "Initial planned entries for this tree.",
            },
        },
    },
    {
        "name": "kb_get_tree_status",
        "description": (
            "Report which trees in the strategy have articles and which are empty. "
            "Useful for gap analysis — identifies planned-but-unfilled sections."
        ),
        "parameters": {},
    },
]

KB_STRATEGY_TOOL_NAMES = {t["name"] for t in KB_STRATEGY_TOOLS}


class KBStrategyToolkit(Toolkit):
    """Tools for reading and updating a KB's organization strategy."""

    def __init__(self, knowledgebase: Knowledgebase):
        self.knowledgebase = knowledgebase

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in KB_STRATEGY_TOOL_NAMES

    def get_tools_schema(self, adapter: ToolAdapter) -> list[dict]:
        from django_ergo.tools import ToolConfig

        schemas = []
        for tool_def in KB_STRATEGY_TOOLS:
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
        if tool_name == "kb_get_strategy":
            return self._get_strategy()
        if tool_name == "kb_update_strategy":
            return self._update_strategy(arguments["strategy"])
        if tool_name == "kb_propose_tree":
            return self._propose_tree(arguments)
        if tool_name == "kb_get_tree_status":
            return self._get_tree_status()
        msg = f"Unknown tool: {tool_name}"
        raise ValueError(msg)

    def get_bound_knowledgebases(self) -> list[tuple]:
        return [(self.knowledgebase, "strategy")]

    def render_overview(self) -> str:
        kb = self.knowledgebase
        strategy_preview = (
            kb.organization_strategy[:STRATEGY_PREVIEW_MAX]
            if kb.organization_strategy
            else "(no strategy set)"
        )
        if len(kb.organization_strategy) > STRATEGY_PREVIEW_MAX:
            strategy_preview += "..."
        return (
            f"=== KB Strategy: {kb.name} (kb_id: {kb.id}) ===\n"
            f"Strategy preview: {strategy_preview}\n"
            f"Available tools: kb_get_strategy, kb_update_strategy, kb_propose_tree, kb_get_tree_status"
        )

    def _get_strategy(self) -> str:
        strategy = self.knowledgebase.organization_strategy
        if not strategy:
            return "No organization strategy has been set for this knowledge base."
        return strategy

    def _update_strategy(self, strategy: str) -> str:
        self.knowledgebase.organization_strategy = strategy
        self.knowledgebase.save(update_fields=["organization_strategy"])
        return f"Organization strategy updated for '{self.knowledgebase.name}' ({len(strategy)} chars)."

    def _propose_tree(self, args: dict) -> str:
        prefix = args["prefix"]
        title = args["title"]
        description = args["description"]
        entries = args.get("entries") or []

        tree_block = f"\n### Tree #{prefix}: {title}\n- {description}\n"
        if entries:
            for entry in entries:
                tree_block += f"  - #{prefix}XX: {entry}\n"

        kb = self.knowledgebase
        kb.organization_strategy = (kb.organization_strategy or "") + tree_block
        kb.save(update_fields=["organization_strategy"])
        return (
            f"Proposed tree #{prefix} ({title}) appended to strategy for '{kb.name}'."
        )

    def _get_tree_status(self) -> str:
        kb = self.knowledgebase
        strategy = kb.organization_strategy
        if not strategy:
            return "No organization strategy set — cannot report tree status."

        # Extract tree prefixes from strategy (patterns like #XX or Tree #XX)
        tree_prefixes = set(re.findall(r"Tree #([0-9A-Fa-f]{2})", strategy))
        # Also match ### Tree #XX: patterns
        tree_prefixes.update(re.findall(r"###\s+Tree\s+#([0-9A-Fa-f]{2})", strategy))
        # Also match #XXYY patterns to infer tree prefixes
        hierarchy_refs = re.findall(r"#([0-9A-Fa-f]{2})(?:XX|[0-9A-Fa-f]{2})", strategy)
        tree_prefixes.update(hierarchy_refs)

        if not tree_prefixes:
            return "No tree prefixes found in the strategy document."

        # Get article counts per prefix
        all_codes = list(kb.articles.values_list("hierarchy_code", flat=True))
        lines = []
        for prefix in sorted(tree_prefixes):
            matching = [c for c in all_codes if c.startswith(prefix)]
            status = f"{len(matching)} articles" if matching else "EMPTY"
            lines.append(f"  Tree #{prefix}: {status}")

        return "Tree status for '{}':\n{}".format(kb.name, "\n".join(lines))
