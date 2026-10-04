"""Toolkit protocol — base class for all scoped tool bundles."""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING


@dataclass(frozen=True)
class ApprovalPreview:
    """Bounded, safe information shown before an approval decision."""

    text: str
    is_error: bool = False


if TYPE_CHECKING:
    from django_ergo.conversation.adapters import ToolAdapter
    from django_ergo.conversation.structured import PreSeedCall


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

    def requires_approval(self, tool_name: str) -> bool:
        """Whether a person must approve this call before it runs.

        The runner yields PendingApproval instead of running it, and
        resume_conversation_turn() continues once there's a decision.
        """
        return False

    def approval_preview(
        self, tool_name: str, arguments: dict
    ) -> ApprovalPreview | None:
        """Return information to show before approving a tool call, if any."""
        return None

    def pre_seeds(self) -> list[PreSeedCall]:
        """Tool calls to run and write into the chat before the first model call.

        The model sees each result as if it had made the call itself, so it
        starts a session already knowing, say, which bots or skills exist.
        """
        return []

    def get_bound_knowledgebases(self) -> list[tuple]:
        """Return [(knowledgebase, mode), ...] for usage tracking.

        Override in KB toolkits. Default returns empty list.
        """
        return []
