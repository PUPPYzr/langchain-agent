"""Compile platform Agent specifications into executable graphs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from langchain.agents import create_agent

from agent_platform.checkpoints import CheckpointProvider, NullCheckpointProvider
from agent_platform.models import ModelProvider
from agent_platform.spec import AgentSpec, validate_agent_spec
from agent_platform.tools import ToolProvider
from agent_platform.state import LangGraphAgentState


@dataclass(frozen=True, slots=True)
class CompiledAgent:
    """A validated AgentSpec paired with its executable runtime object."""

    spec: AgentSpec
    executable: Any


class AgentCompiler(Protocol):
    """Compile a platform AgentSpec without leaking framework setup to callers."""

    def compile(self, spec: AgentSpec) -> CompiledAgent:
        """Validate and compile one Agent specification."""


class LangChainAgentCompiler:
    """Compile AgentSpec through LangChain's LangGraph-backed agent factory."""

    def __init__(
        self,
        *,
        model_provider: ModelProvider,
        tool_provider: ToolProvider,
        checkpoint_provider: CheckpointProvider | None = None,
    ) -> None:
        self._model_provider = model_provider
        self._tool_provider = tool_provider
        self._checkpoint_provider = checkpoint_provider or NullCheckpointProvider()

    def compile(self, spec: AgentSpec) -> CompiledAgent:
        validate_agent_spec(spec, self._tool_provider)
        model = self._model_provider.create_chat_model(spec.model)
        tools = [
            self._tool_provider.resolve(reference.tool_id, reference.version).implementation
            for reference in spec.tools
        ]
        executable = create_agent(
            model=model,
            tools=tools,
            system_prompt=spec.prompt.system,
            name=spec.agent_id,
            state_schema=LangGraphAgentState,
            checkpointer=self._checkpoint_provider.create_checkpointer(),
        )
        return CompiledAgent(spec=spec, executable=executable)
