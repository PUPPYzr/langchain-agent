"""Shared execution behavior for graph-compatible Agent runtimes."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Callable

from agent_platform.domain import (
    AgentContext,
    AgentDefinition,
    AgentRunResult,
    AgentRuntimeError,
)
from agent_platform.events import AgentEvent, EventSink


class ExecutableAgentRuntime:
    """Execute an invoke/stream compatible Agent without duplicating adapters."""

    def __init__(
        self,
        agent_factory: Callable[[], Any],
        *,
        runtime_type: str,
    ) -> None:
        self._agent_factory = agent_factory
        self.runtime_type = runtime_type
        self.supports_resume = False

    def run(
        self,
        agent: AgentDefinition,
        context: AgentContext,
    ) -> AgentRunResult:
        self._validate(agent, context)
        self._emit(context, "run.started", runtime_type=self.runtime_type)
        try:
            result = self._agent_factory().invoke(
                self._input(context),
                config=self._runtime_config(context),
            )
            content = self._extract_content(result)
        except Exception as exc:
            self._emit(context, "run.failed", error_type=type(exc).__name__)
            raise AgentRuntimeError(
                f"{self.runtime_type} Agent execution failed."
            ) from exc
        self._emit(context, "run.completed", content=content)
        return self._result(context, content)

    def stream(
        self,
        agent: AgentDefinition,
        context: AgentContext,
    ) -> Iterator[AgentEvent]:
        self._validate(agent, context)
        started = self._event(context, "run.started", runtime_type=self.runtime_type)
        self._publish(context, started)
        yield started
        last_state: Any = None
        try:
            for state in self._agent_factory().stream(
                self._input(context),
                config=self._runtime_config(context),
                stream_mode="values",
            ):
                last_state = state
                event = self._event(context, "state.updated", state=state)
                self._publish(context, event)
                yield event
            content = self._extract_content(last_state)
        except Exception as exc:
            failed = self._event(
                context,
                "run.failed",
                error_type=type(exc).__name__,
            )
            self._publish(context, failed)
            yield failed
            raise AgentRuntimeError(
                f"{self.runtime_type} Agent streaming failed."
            ) from exc
        completed = self._event(context, "run.completed", content=content)
        self._publish(context, completed)
        yield completed

    def resume(
        self,
        agent: AgentDefinition,
        context: AgentContext,
    ) -> AgentRunResult:
        """Reject resume unless a concrete Runtime implements it."""
        self._validate(agent, context)
        raise AgentRuntimeError(
            f"Runtime does not support resume: {self.runtime_type}"
        )

    def _validate(self, agent: AgentDefinition, context: AgentContext) -> None:
        if agent.runtime_type != self.runtime_type:
            raise AgentRuntimeError(
                f"Unsupported runtime type for {type(self).__name__}: "
                f"{agent.runtime_type}"
            )
        if not context.question.strip():
            raise AgentRuntimeError("Agent question must not be empty.")

    @staticmethod
    def _input(context: AgentContext) -> dict[str, Any]:
        return {
            "messages": [
                {
                    "role": "user",
                    "content": context.question,
                }
            ]
        }

    @staticmethod
    def _runtime_config(context: AgentContext) -> Any:
        config = dict(context.metadata.get("runtime_config") or {})
        configurable = dict(config.get("configurable") or {})
        configurable.setdefault("thread_id", context.session_id)
        config["configurable"] = configurable
        return config

    def _result(self, context: AgentContext, content: str) -> AgentRunResult:
        return AgentRunResult(
            run_id=context.run_id,
            session_id=context.session_id,
            content=content,
            metadata={"runtime_type": self.runtime_type},
        )

    @staticmethod
    def _extract_content(result: Any) -> str:
        if not isinstance(result, dict):
            raise AgentRuntimeError("Agent returned an invalid result.")
        messages = result.get("messages") or []
        if not messages:
            raise AgentRuntimeError("Agent returned no messages.")
        message = messages[-1]
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        return content if isinstance(content, str) else str(content)

    @staticmethod
    def _sink(context: AgentContext) -> EventSink | None:
        sink = context.metadata.get("event_sink")
        return sink if hasattr(sink, "publish") else None

    def _event(self, context: AgentContext, event_type: str, **data: Any) -> AgentEvent:
        return AgentEvent(
            event_type=event_type,
            run_id=context.run_id,
            session_id=context.session_id,
            data=data,
        )

    def _publish(self, context: AgentContext, event: AgentEvent) -> None:
        sink = self._sink(context)
        if sink is not None:
            sink.publish(event)

    def _emit(self, context: AgentContext, event_type: str, **data: Any) -> None:
        self._publish(context, self._event(context, event_type, **data))
