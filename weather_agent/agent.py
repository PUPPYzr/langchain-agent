"""Weather agent construction."""

from __future__ import annotations

from typing import Any

from agent_platform.checkpoints import CheckpointProvider, SQLiteCheckpointProvider
from agent_platform.compiler import CompiledAgent, LangChainAgentCompiler
from agent_platform.models import LangChainModelProvider, ModelProvider
from agent_platform.spec import AgentSpec
from agent_platform.tools import ToolRegistry
from weather_agent.config import Settings
from weather_agent.agent_spec import build_weather_agent_spec
from weather_agent.tool_registry import WEATHER_TOOL_REGISTRY


def build_weather_agent(
    settings: Settings | None = None,
    *,
    tool_registry: ToolRegistry | None = None,
    model_provider: ModelProvider | None = None,
    checkpoint_provider: CheckpointProvider | None = None,
    agent_spec: AgentSpec | None = None,
) -> Any:
    """Build and return the configured LangChain weather agent."""
    return build_compiled_weather_agent(
        settings,
        tool_registry=tool_registry,
        model_provider=model_provider,
        checkpoint_provider=checkpoint_provider,
        agent_spec=agent_spec,
    ).executable


def build_compiled_weather_agent(
    settings: Settings | None = None,
    *,
    tool_registry: ToolRegistry | None = None,
    model_provider: ModelProvider | None = None,
    checkpoint_provider: CheckpointProvider | None = None,
    agent_spec: AgentSpec | None = None,
) -> CompiledAgent:
    """Build the validated weather Agent specification and executable graph."""
    resolved_settings = settings or Settings.from_environment()
    resolved_tool_registry = tool_registry or WEATHER_TOOL_REGISTRY
    resolved_spec = agent_spec or build_weather_agent_spec(
        resolved_settings,
        tool_registry=resolved_tool_registry,
    )
    resolved_model_provider = model_provider or LangChainModelProvider(
        api_key=resolved_settings.openai_api_key,
        base_url=resolved_settings.openai_base_url,
    )
    resolved_checkpoint_provider = checkpoint_provider
    if resolved_checkpoint_provider is None and resolved_spec.runtime_type == "langgraph":
        resolved_checkpoint_provider = SQLiteCheckpointProvider(
            resolved_settings.agent_checkpoint_db_path
        )
    compiler = LangChainAgentCompiler(
        model_provider=resolved_model_provider,
        tool_provider=resolved_tool_registry,
        checkpoint_provider=resolved_checkpoint_provider,
    )
    return compiler.compile(resolved_spec)
