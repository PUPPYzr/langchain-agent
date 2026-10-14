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
from agent_platform.guardrails import ToolCallGuard, active_tool_guard


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
            result, guard = self._guarded_call(
                context,
                lambda: self._agent_factory().invoke(
                    self._input(context),
                    config=self._runtime_config(context),
                ),
            )
            run_result = self._result_from_output(context, result)
        except Exception as exc:
            self._emit(context, "run.failed", error_type=type(exc).__name__)
            raise AgentRuntimeError(
                f"{self.runtime_type} Agent execution failed."
            ) from exc
        run_result = self._merge_guard_metadata(run_result, guard)
        self._emit_result_event(context, run_result)
        return run_result

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
        guard = self._tool_call_guard(context)
        try:
            with active_tool_guard(guard):
                for state in self._agent_factory().stream(
                    self._input(context),
                    config=self._runtime_config(context),
                    stream_mode="values",
                ):
                    last_state = state
                    event = self._event(context, "state.updated", state=state)
                    self._publish(context, event)
                    yield event
            run_result = self._result_from_output(context, last_state)
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
        run_result = self._merge_guard_metadata(run_result, guard)
        event_type = (
            "run.interrupted"
            if run_result.status == "interrupted"
            else "run.completed"
        )
        completed = self._event(
            context,
            event_type,
            content=run_result.content,
            **dict(run_result.metadata),
        )
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

    @staticmethod
    def _tool_call_guard(context: AgentContext) -> ToolCallGuard:
        """Build a fresh per-run guard from the optional tool-call policy."""
        policy = dict(context.metadata.get("tool_call_policy") or {})
        max_tool_calls = policy.get("max_tool_calls")
        return ToolCallGuard(
            max_tool_calls=int(max_tool_calls) if max_tool_calls else None
        )

    def _guarded_call(self, context: AgentContext, operation: Any) -> tuple[Any, ToolCallGuard]:
        """Run one operation with the per-run tool-call guard bound."""
        guard = self._tool_call_guard(context)
        with active_tool_guard(guard):
            return operation(), guard

    @staticmethod
    def _merge_guard_metadata(
        result: AgentRunResult,
        guard: ToolCallGuard,
    ) -> AgentRunResult:
        """Attach tool-call policy counters to the Run result metadata."""
        metadata = dict(result.metadata)
        metadata["tool_call_policy"] = guard.summary()
        return AgentRunResult(
            run_id=result.run_id,
            session_id=result.session_id,
            content=result.content,
            status=result.status,
            metadata=metadata,
        )

    def _result(
        self,
        context: AgentContext,
        content: str,
        *,
        status: str = "completed",
        metadata: dict[str, Any] | None = None,
    ) -> AgentRunResult:
        return AgentRunResult(
            run_id=context.run_id,
            session_id=context.session_id,
            content=content,
            status=status,
            metadata={"runtime_type": self.runtime_type, **(metadata or {})},
        )

    def _result_from_output(
        self,
        context: AgentContext,
        output: Any,
    ) -> AgentRunResult:
        interrupts = self._interrupts(output)
        if interrupts:
            return self._result(
                context,
                "",
                status="interrupted",
                metadata={"interrupts": interrupts},
            )
        return self._result(context, self._extract_content(output))

    def _emit_result_event(
        self,
        context: AgentContext,
        result: AgentRunResult,
    ) -> None:
        event_type = (
            "run.interrupted" if result.status == "interrupted" else "run.completed"
        )
        self._emit(
            context,
            event_type,
            content=result.content,
            **dict(result.metadata),
        )

    @staticmethod
    def _interrupts(output: Any) -> list[dict[str, Any]]:
        if not isinstance(output, dict):
            return []
        interrupts = output.get("__interrupt__") or ()
        return [
            {
                "id": str(getattr(item, "id", "")),
                "value": ExecutableAgentRuntime._json_safe(
                    getattr(item, "value", item)
                ),
            }
            for item in interrupts
        ]

    @staticmethod
    def _json_safe(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {
                str(key): ExecutableAgentRuntime._json_safe(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [ExecutableAgentRuntime._json_safe(item) for item in value]
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return ExecutableAgentRuntime._json_safe(model_dump())
        return str(value)

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
