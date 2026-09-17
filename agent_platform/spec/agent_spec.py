"""Framework-independent, versioned Agent specification."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from agent_platform.models import ModelSpec
from agent_platform.domain import AgentDefinition
from agent_platform.spec.prompt_spec import PromptSpec


@dataclass(frozen=True, slots=True)
class ToolReference:
    """A stable reference to a registered Tool implementation."""

    tool_id: str
    version: int = 1

    def __post_init__(self) -> None:
        if not self.tool_id.strip():
            raise ValueError("tool_id must not be empty")
        if self.version < 1:
            raise ValueError("tool version must be at least 1")


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """Versioned platform definition compiled by a Runtime or Compiler."""

    agent_id: str
    version: int
    goal: str
    model: ModelSpec
    prompt: PromptSpec
    tools: tuple[ToolReference, ...]
    runtime_type: str = "legacy"
    max_turns: int = 4
    timeout_seconds: float = 30.0
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.agent_id.strip():
            raise ValueError("agent_id must not be empty")
        if self.version < 1:
            raise ValueError("Agent version must be at least 1")
        if not self.goal.strip():
            raise ValueError("goal must not be empty")
        if not self.tools:
            raise ValueError("Agent must reference at least one Tool")
        if self.max_turns < 1 or self.max_turns > 20:
            raise ValueError("max_turns must be between 1 and 20")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")

    @property
    def tool_ids(self) -> tuple[str, ...]:
        """Return the stable Tool IDs referenced by this specification."""
        return tuple(reference.tool_id for reference in self.tools)

    def to_definition(self) -> AgentDefinition:
        """Project the versioned spec into the framework-independent runtime definition."""
        return AgentDefinition(
            agent_id=self.agent_id,
            version=self.version,
            goal=self.goal,
            runtime_type=self.runtime_type,
            model_name=self.model.model,
            tool_ids=self.tool_ids,
            metadata={"spec_version": str(self.version)},
        )
