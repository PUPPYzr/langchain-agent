"""Explicit LangGraph Runtime implementation."""

from __future__ import annotations

from typing import Any, Callable

from langgraph.types import Command

from agent_platform.domain import AgentContext, AgentDefinition, AgentRunResult
from agent_platform.executable_runtime import ExecutableAgentRuntime


class LangGraphAgentRuntime(ExecutableAgentRuntime):
    """Execute and resume a LangGraph-compatible compiled Agent."""

    def __init__(self, agent_factory: Callable[[], Any]) -> None:
        super().__init__(agent_factory, runtime_type="langgraph")
        self.supports_resume = True

    def resume(
        self,
        agent: AgentDefinition,
        context: AgentContext,
    ) -> AgentRunResult:
        self._validate(agent, context)
        self._emit(context, "run.resumed", runtime_type=self.runtime_type)
        try:
            resume_value = context.metadata.get("resume_value")
            input_payload = (
                Command(resume=resume_value)
                if "resume_value" in context.metadata
                else None
            )
            result = self._agent_factory().invoke(
                input_payload,
                config=self._runtime_config(context),
            )
            content = self._extract_content(result)
        except Exception as exc:
            self._emit(context, "run.failed", error_type=type(exc).__name__)
            from agent_platform.domain import AgentRuntimeError

            raise AgentRuntimeError("LangGraph Agent resume failed.") from exc
        self._emit(context, "run.completed", content=content)
        return self._result(context, content)
