"""Legacy Runtime adapter for the existing LangChain Agent implementation."""

from __future__ import annotations

from typing import Any, Callable

from agent_platform.executable_runtime import ExecutableAgentRuntime


class LegacyAgentRuntime(ExecutableAgentRuntime):
    """Run the existing LangChain Agent behind the platform Runtime contract.

    The adapter owns framework-specific invocation details while the CLI and
    future platform services depend only on AgentRuntime-shaped behavior.
    """

    def __init__(self, agent_factory: Callable[[], Any]) -> None:
        super().__init__(agent_factory, runtime_type="legacy")
