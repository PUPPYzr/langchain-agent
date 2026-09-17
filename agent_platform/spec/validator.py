"""Validation for platform-owned Agent specifications."""

from __future__ import annotations

from dataclasses import dataclass

from agent_platform.spec.agent_spec import AgentSpec
from agent_platform.tools import ToolProvider


@dataclass(frozen=True, slots=True)
class AgentSpecValidationError(ValueError):
    """A validation error with a stable field identifier."""

    field: str
    reason: str

    def __str__(self) -> str:
        return f"Invalid AgentSpec field '{self.field}': {self.reason}"


class AgentSpecValidator:
    """Validate an AgentSpec against platform runtime and Tool contracts."""

    SUPPORTED_RUNTIMES = frozenset({"legacy", "langgraph"})

    def validate(self, spec: AgentSpec, tool_registry: ToolProvider) -> None:
        if spec.runtime_type not in self.SUPPORTED_RUNTIMES:
            raise AgentSpecValidationError(
                "runtime_type",
                f"unsupported runtime: {spec.runtime_type}",
            )
        if spec.timeout_seconds < spec.model.timeout_seconds:
            raise AgentSpecValidationError(
                "timeout_seconds",
                "Agent timeout must be at least the model timeout",
            )
        if spec.max_turns > 20:
            raise AgentSpecValidationError(
                "max_turns",
                "must not be greater than 20",
            )
        for reference in spec.tools:
            try:
                registered = tool_registry.resolve(
                    reference.tool_id,
                    reference.version,
                )
            except KeyError as exc:
                raise AgentSpecValidationError(
                    "tools",
                    str(exc),
                ) from exc
            if registered.spec.input_schema is None:
                raise AgentSpecValidationError(
                    f"tools.{reference.tool_id}.input_schema",
                    "must be declared",
                )
            if registered.spec.output_schema is None:
                raise AgentSpecValidationError(
                    f"tools.{reference.tool_id}.output_schema",
                    "must be declared",
                )


def validate_agent_spec(spec: AgentSpec, tool_registry: ToolProvider) -> AgentSpec:
    """Validate and return the same spec for fluent construction paths."""
    AgentSpecValidator().validate(spec, tool_registry)
    return spec
