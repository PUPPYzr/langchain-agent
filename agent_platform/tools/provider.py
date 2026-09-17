"""Platform Tool provider protocol."""

from __future__ import annotations

from typing import Protocol

from agent_platform.tools.registry import RegisteredTool
from agent_platform.tools.spec import ToolSpec


class ToolProvider(Protocol):
    """Resolve executable Tools from platform-owned specifications."""

    def register(self, spec: ToolSpec) -> None:
        """Register one versioned Tool."""

    def resolve(self, tool_id: str, version: int = 1) -> RegisteredTool:
        """Resolve one Tool by stable ID and version."""
