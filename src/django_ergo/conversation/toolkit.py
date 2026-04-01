"""Toolkit protocol — base class for all scoped tool bundles."""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django_ergo.conversation.adapters import ToolAdapter


class Toolkit(ABC):
    """Abstract base for scoped tool bundles.

    A toolkit is a set of tools bound to specific data (e.g., knowledgebases,
    conversation sessions) with a defined capability scope. Toolkits plug into
    the conversation runner via the extra_tools parameter.
    """

    @abstractmethod
    def has_tool(self, tool_name: str) -> bool:
        """Check if this toolkit handles a given tool name."""

    @abstractmethod
    def execute_tool(self, tool_name: str, arguments: dict) -> str:
        """Execute a toolkit tool and return the result string."""

    @abstractmethod
    def get_tools_schema(self, adapter: ToolAdapter) -> list[dict]:
        """Return tool schemas in engine-native format."""

    @abstractmethod
    def render_overview(self) -> str:
        """Render initial context for the agent (e.g., TOC, summaries)."""
