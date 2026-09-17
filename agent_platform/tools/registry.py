"""In-memory Tool Registry for the current single-process runtime."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from agent_platform.tools.spec import ToolSpec


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """A ToolSpec and its resolved executable implementation."""

    spec: ToolSpec

    @property
    def implementation(self) -> Any:
        return self.spec.implementation


class ToolRegistry:
    """Resolve versioned Tool IDs without exposing storage details to Agents."""

    def __init__(self, tools: Iterable[ToolSpec] = ()) -> None:
        self._tools: dict[tuple[str, int], RegisteredTool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, spec: ToolSpec) -> None:
        key = (spec.tool_id, spec.version)
        if key in self._tools:
            raise ValueError(
                f"Tool already registered: {spec.tool_id} v{spec.version}"
            )
        self._tools[key] = RegisteredTool(spec=spec)

    def resolve(self, tool_id: str, version: int = 1) -> RegisteredTool:
        try:
            return self._tools[(tool_id, version)]
        except KeyError as exc:
            raise KeyError(f"Tool not found: {tool_id} v{version}") from exc

    def resolve_many(self, tool_ids: Iterable[str], version: int = 1) -> list[RegisteredTool]:
        return [self.resolve(tool_id, version) for tool_id in tool_ids]

    def list_specs(self) -> tuple[ToolSpec, ...]:
        return tuple(registered.spec for registered in self._tools.values())
