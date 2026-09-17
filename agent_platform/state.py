"""Platform-owned Agent state contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, NotRequired

from langchain.agents import AgentState as LangChainAgentState


@dataclass(frozen=True, slots=True)
class ExecutionState:
    """Execution metadata kept separate from conversation messages."""

    status: str = "pending"
    current_step: str | None = None
    turn_count: int = 0
    error_count: int = 0


@dataclass(frozen=True, slots=True)
class AgentState:
    """Framework-independent state shared by platform runtimes."""

    messages: tuple[Any, ...] = ()
    user_context: Mapping[str, Any] = field(default_factory=dict)
    variables: Mapping[str, Any] = field(default_factory=dict)
    tool_results: tuple[Any, ...] = ()
    artifacts: tuple[Any, ...] = ()
    errors: tuple[Any, ...] = ()
    execution: ExecutionState = field(default_factory=ExecutionState)
    metadata: Mapping[str, Any] = field(default_factory=dict)


class LangGraphAgentState(LangChainAgentState, total=False):
    """LangGraph-compatible state schema carrying platform execution fields."""

    user_context: NotRequired[dict[str, Any]]
    variables: NotRequired[dict[str, Any]]
    tool_results: NotRequired[list[Any]]
    artifacts: NotRequired[list[Any]]
    errors: NotRequired[list[Any]]
    execution: NotRequired[dict[str, Any]]
    metadata: NotRequired[dict[str, Any]]
