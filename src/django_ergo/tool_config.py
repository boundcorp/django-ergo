"""Provider- and model-independent tool schema, shared with the legacy registry."""

from dataclasses import dataclass
from typing import Any


@dataclass
class ToolConfig:
    name: str
    description: str
    parameters: dict[str, Any]
    requires_approval: bool = False
    readonly: bool = False
