"""Framework-independent Agent domain contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

RunStatus = Literal["pending", "running", "completed", "failed", "cancelled"]


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """Versioned platform definition for one executable Agent."""

    agent_id: str
    version: int
    goal: str
    runtime_type: str = "legacy"
    model_name: str | None = None
    tool_ids: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentContext:
    """Input context supplied to a Runtime for one run."""

    question: str
    run_id: str
    session_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """Framework-independent result returned by a Runtime."""

    run_id: str
    session_id: str
    content: str
    status: RunStatus = "completed"
    metadata: Mapping[str, Any] = field(default_factory=dict)


class AgentRuntimeError(RuntimeError):
    """Raised when a platform Runtime cannot execute an Agent."""


class AgentRunTimeoutError(AgentRuntimeError):
    """Raised when a Run exceeds its configured deadline."""


class AgentRunCancelledError(AgentRuntimeError):
    """Raised when a Run is cancelled before completion."""
