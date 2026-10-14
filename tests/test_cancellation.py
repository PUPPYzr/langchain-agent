"""Tests for cooperative cancellation semantics."""

import time
import unittest
from threading import Event

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool as make_tool

from agent_platform.application import AgentApplicationService
from agent_platform.cancellation import CancellationToken, cancellation_scope
from agent_platform.compiler import CompiledAgent, LangChainAgentCompiler
from agent_platform.domain import (
    AgentContext,
    AgentRunCancelledError,
    AgentRunTimeoutError,
)
from agent_platform.guardrails import ToolCallGuard, active_tool_guard, make_guarded_tool
from agent_platform.models import ModelSpec
from agent_platform.runs import InMemoryRunRepository
from agent_platform.spec import AgentSpec, PromptSpec, ToolReference
from agent_platform.tools import ToolRegistry, ToolSpec


def _tool_call(call_id: str, location: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "lookup_weather",
                "args": {"location": location},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


class BindableFakeMessagesListChatModel(FakeMessagesListChatModel):
    """Fake model that ignores tool binding and replays scripted messages."""

    def bind_tools(self, tools, **kwargs):
        return self


class CancellationTokenTests(unittest.TestCase):
    def test_token_wraps_an_external_event(self) -> None:
        event = Event()
        token = CancellationToken(event)

        self.assertFalse(token.cancelled)
        event.set()
        self.assertTrue(token.cancelled)

    def test_request_cancel_records_the_first_reason(self) -> None:
        token = CancellationToken()

        token.request_cancel("run timeout")
        token.request_cancel("later reason")

        self.assertTrue(token.cancelled)
        self.assertEqual(token.reason, "run timeout")

    def test_raise_if_cancelled_reports_the_stage(self) -> None:
        token = CancellationToken(Event())
        token.request_cancel("test")

        with self.assertRaises(AgentRunCancelledError) as captured:
            token.raise_if_cancelled("execution")

        self.assertIn("before execution", str(captured.exception))


class CancelDuringRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[str] = []
        self.cancel_event = Event()

        @make_tool
        def lookup_weather(location: str) -> str:
            """Return a stub weather summary and request cancellation."""
            self.calls.append(location)
            self.cancel_event.set()
            return f"{location}:晴朗,20C"

        self.registry = ToolRegistry(
            [
                ToolSpec(
                    tool_id="lookup-weather",
                    version=1,
                    description="stub weather lookup",
                    implementation=lookup_weather,
                    input_schema=dict,
                    output_schema=dict,
                )
            ]
        )
        self.spec = AgentSpec(
            agent_id="cancel-agent",
            version=1,
            goal="test cancellation",
            model=ModelSpec(provider="openai-compatible", model="test-model"),
            prompt=PromptSpec(prompt_id="cancel", version=1, system="test"),
            tools=(ToolReference("lookup-weather"),),
            runtime_type="langgraph",
        )

    def _compile(self, messages) -> CompiledAgent:
        model = BindableFakeMessagesListChatModel(responses=messages)

        class FakeModelProvider:
            def create_chat_model(self, spec):
                return model

        return LangChainAgentCompiler(
            model_provider=FakeModelProvider(),
            tool_provider=self.registry,
        ).compile(self.spec)

    def test_cancel_during_run_blocks_further_tool_calls(self) -> None:
        compiled = self._compile(
            [
                _tool_call("call-1", "合肥"),
                _tool_call("call-2", "上海"),
                AIMessage(content="已基于已有信息作答。"),
            ]
        )
        repository = InMemoryRunRepository()
        context = AgentContext(
            question="合肥和上海天气",
            run_id="run-cancel-mid",
            session_id="thread-cancel-mid",
            metadata={"cancel_event": self.cancel_event},
        )

        with self.assertRaises(AgentRunCancelledError):
            AgentApplicationService(run_repository=repository).run(compiled, context)

        self.assertEqual(self.calls, ["合肥"])
        self.assertEqual(repository.get("run-cancel-mid").status, "cancelled")

    def test_cancel_before_execution_does_not_call_the_tool(self) -> None:
        compiled = self._compile([_tool_call("call-1", "合肥")])
        self.cancel_event.set()
        context = AgentContext(
            question="合肥天气",
            run_id="run-cancel-pre",
            session_id="thread-cancel-pre",
            metadata={"cancel_event": self.cancel_event},
        )

        with self.assertRaises(AgentRunCancelledError):
            AgentApplicationService().run(compiled, context)

        self.assertEqual(self.calls, [])


class StreamingCancelTests(unittest.TestCase):
    def test_stream_stops_at_the_next_event_after_cancel(self) -> None:
        cancel_event = Event()

        class StreamingExecutable:
            def stream(self, payload, *, config=None, stream_mode=None):
                yield {"messages": [{"content": "first"}]}
                cancel_event.set()
                yield {"messages": [{"content": "second"}]}
                yield {"messages": [{"content": "third"}]}

        spec = AgentSpec(
            agent_id="stream-agent",
            version=1,
            goal="test stream cancel",
            model=ModelSpec(provider="openai-compatible", model="test-model"),
            prompt=PromptSpec(prompt_id="stream", version=1, system="test"),
            tools=(ToolReference("lookup-weather"),),
            runtime_type="langgraph",
        )
        repository = InMemoryRunRepository()
        service = AgentApplicationService(run_repository=repository)
        context = AgentContext(
            question="合肥天气",
            run_id="run-stream-cancel",
            session_id="thread-stream-cancel",
            metadata={"cancel_event": cancel_event},
        )
        collected = []

        with self.assertRaises(AgentRunCancelledError):
            for event in service.stream(
                CompiledAgent(spec=spec, executable=StreamingExecutable()),
                context,
            ):
                collected.append(event)

        self.assertTrue(collected)
        self.assertEqual(repository.get("run-stream-cancel").status, "cancelled")


class TimeoutReportingTests(unittest.TestCase):
    def test_timeout_reports_that_in_flight_calls_may_still_be_draining(self) -> None:
        class SlowExecutable:
            def invoke(self, payload, *, config=None):
                time.sleep(0.05)
                return {"messages": [{"content": "late"}]}

        spec = AgentSpec(
            agent_id="slow-agent",
            version=1,
            goal="test timeout",
            model=ModelSpec(provider="openai-compatible", model="test-model"),
            prompt=PromptSpec(prompt_id="slow", version=1, system="test"),
            tools=(ToolReference("lookup-weather"),),
            runtime_type="langgraph",
            timeout_seconds=0.001,
        )
        context = AgentContext(
            question="合肥天气",
            run_id="run-timeout",
            session_id="thread-timeout",
        )

        with self.assertRaises(AgentRunTimeoutError) as captured:
            AgentApplicationService().run(
                CompiledAgent(spec=spec, executable=SlowExecutable()),
                context,
            )

        self.assertIn("draining", str(captured.exception))


class GuardCancellationTests(unittest.TestCase):
    def test_guard_blocks_calls_once_cancelled(self) -> None:
        calls: list[str] = []

        @make_tool
        def echo(value: str) -> str:
            """Echo a value."""
            calls.append(value)
            return value

        guarded = make_guarded_tool(echo)
        token = CancellationToken()
        token.request_cancel("test")

        with active_tool_guard(ToolCallGuard()):
            with cancellation_scope(token):
                result = guarded.invoke({"value": "a"})

        self.assertEqual(calls, [])
        self.assertIn("cancelled", result)


if __name__ == "__main__":
    unittest.main()
