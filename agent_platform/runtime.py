"""Platform-owned Runtime interface."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Protocol

from agent_platform.domain import AgentContext, AgentDefinition, AgentRunResult
from agent_platform.events import AgentEvent


class AgentRuntime(Protocol):
    """Runtime contract independent of LangChain or LangGraph."""

    def run(
        self,
        agent: AgentDefinition,
        context: AgentContext,
    ) -> AgentRunResult:
        """Execute one Agent run and return a platform result."""

    def stream(
        self,
        agent: AgentDefinition,
        context: AgentContext,
    ) -> Iterator[AgentEvent]:
        """Stream normalized events for one Agent run."""

    def resume(
        self,
        agent: AgentDefinition,
        context: AgentContext,
    ) -> AgentRunResult:
        """Resume a checkpointed run when the Runtime supports it."""


class RuntimeCapabilities(Protocol):
    """Optional capability markers exposed by concrete runtimes."""

    runtime_type: str
    supports_resume: bool


RuntimeFactory = Callable[[Callable[[], object]], AgentRuntime]


class RuntimeRegistry:
    """Resolve Runtime factories by stable platform runtime type."""

    def __init__(self) -> None:
        self._factories: dict[str, RuntimeFactory] = {}

    def register(self, runtime_type: str, factory: RuntimeFactory) -> None:
        normalized = runtime_type.strip()
        if not normalized:
            raise ValueError("runtime_type must not be empty")
        if normalized in self._factories:
            raise ValueError(f"Runtime already registered: {normalized}")
        self._factories[normalized] = factory

    def create(
        self,
        runtime_type: str,
        agent_factory: Callable[[], object],
    ) -> AgentRuntime:
        try:
            factory = self._factories[runtime_type]
        except KeyError as exc:
            raise KeyError(f"Runtime not found: {runtime_type}") from exc
        return factory(agent_factory)

    @classmethod
    def with_defaults(cls) -> "RuntimeRegistry":
        from agent_platform.langgraph_runtime import LangGraphAgentRuntime
        from agent_platform.legacy_runtime import LegacyAgentRuntime

        registry = cls()
        registry.register("legacy", LegacyAgentRuntime)
        registry.register("langgraph", LangGraphAgentRuntime)
        return registry
