"""Real tool-loop integration tests and platform tool-call policy tests."""

import unittest

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool as make_tool

from agent_platform.application import AgentApplicationService
from agent_platform.compiler import CompiledAgent, LangChainAgentCompiler
from agent_platform.domain import AgentContext
from agent_platform.guardrails import ToolCallGuard, active_tool_guard, make_guarded_tool
from agent_platform.langgraph_runtime import LangGraphAgentRuntime
from agent_platform.models import ModelSpec
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


class RecordingToolLoopTests(unittest.TestCase):
    """Execute a real LangGraph agent loop through the platform runtime."""

    def setUp(self) -> None:
        self.calls: list[str] = []

        @make_tool
        def lookup_weather(location: str) -> str:
            """Return a stub weather summary for a location."""
            self.calls.append(location)
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
            agent_id="loop-agent",
            version=1,
            goal="test tool loop",
            model=ModelSpec(provider="openai-compatible", model="test-model"),
            prompt=PromptSpec(prompt_id="loop", version=1, system="test"),
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

    def test_real_multi_turn_tool_loop_executes_tool_and_answers(self) -> None:
        compiled = self._compile(
            [
                _tool_call("call-1", "合肥"),
                AIMessage(content="合肥当前天气：晴朗，20℃。"),
                AIMessage(content="合肥当前天气：晴朗，20℃。"),
            ]
        )
        context = AgentContext(
            question="合肥天气",
            run_id="run-loop",
            session_id="thread-loop",
        )

        result = LangGraphAgentRuntime(lambda: compiled.executable).run(
            self.spec.to_definition(),
            context,
        )

        self.assertEqual(self.calls, ["合肥"])
        self.assertEqual(result.content, "合肥当前天气：晴朗，20℃。")
        self.assertEqual(
            result.metadata["tool_call_policy"]["tool_call_count"],
            1,
        )

    def test_duplicate_tool_call_is_blocked_without_reexecution(self) -> None:
        compiled = self._compile(
            [
                _tool_call("call-1", "合肥"),
                _tool_call("call-2", "合肥"),
                AIMessage(content="合肥当前天气：晴朗，20℃。"),
            ]
        )
        context = AgentContext(
            question="合肥天气",
            run_id="run-duplicate",
            session_id="thread-duplicate",
            metadata={"tool_call_policy": {"max_tool_calls": 4}},
        )

        result = AgentApplicationService().run(compiled, context)

        self.assertEqual(self.calls, ["合肥"])
        self.assertEqual(result.content, "合肥当前天气：晴朗，20℃。")
        policy = result.metadata["tool_call_policy"]
        self.assertEqual(policy["tool_call_count"], 1)
        self.assertEqual(policy["blocked_duplicates"], 1)

    def test_tool_call_budget_is_enforced(self) -> None:
        compiled = self._compile(
            [
                _tool_call("call-1", "合肥"),
                _tool_call("call-2", "上海"),
                AIMessage(content="两次查询完成。"),
            ]
        )
        context = AgentContext(
            question="合肥和上海天气",
            run_id="run-budget",
            session_id="thread-budget",
            metadata={"tool_call_policy": {"max_tool_calls": 1}},
        )

        result = AgentApplicationService().run(compiled, context)

        self.assertEqual(self.calls, ["合肥"])
        policy = result.metadata["tool_call_policy"]
        self.assertEqual(policy["tool_call_count"], 1)
        self.assertEqual(policy["blocked_over_budget"], 1)


class ToolCallGuardTests(unittest.TestCase):
    def test_identical_call_is_blocked(self) -> None:
        guard = ToolCallGuard()

        self.assertIsNone(guard.check("t", {"location": "合肥"}))
        blocked = guard.check("t", {"location": "合肥"})

        self.assertIsNotNone(blocked)
        self.assertIn("duplicate_tool_call", blocked)
        self.assertEqual(guard.blocked_duplicates, 1)

    def test_dictionary_key_order_does_not_hide_duplicates(self) -> None:
        guard = ToolCallGuard()

        self.assertIsNone(guard.check("t", {"a": 1, "b": 2}))
        self.assertIsNotNone(guard.check("t", {"b": 2, "a": 1}))

    def test_different_arguments_are_allowed(self) -> None:
        guard = ToolCallGuard()

        self.assertIsNone(guard.check("t", {"location": "合肥"}))
        self.assertIsNone(guard.check("t", {"location": "上海"}))

        self.assertEqual(guard.tool_call_count, 2)

    def test_budget_is_enforced(self) -> None:
        guard = ToolCallGuard(max_tool_calls=1)

        self.assertIsNone(guard.check("t", {"location": "合肥"}))
        blocked = guard.check("t", {"location": "上海"})

        self.assertIn("tool_call_budget_exhausted", blocked)
        self.assertEqual(guard.blocked_over_budget, 1)

    def test_invalid_budget_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ToolCallGuard(max_tool_calls=0)


class GuardedToolTests(unittest.TestCase):
    def test_non_tool_implementation_is_returned_unchanged(self) -> None:
        marker = object()

        self.assertIs(make_guarded_tool(marker), marker)

    def test_guarded_tool_blocks_duplicate_without_calling_original(self) -> None:
        calls: list[str] = []

        @make_tool
        def echo(value: str) -> str:
            """Echo a value."""
            calls.append(value)
            return value

        guarded = make_guarded_tool(echo)
        guard = ToolCallGuard()

        with active_tool_guard(guard):
            first = guarded.invoke({"value": "a"})
            second = guarded.invoke({"value": "a"})

        self.assertEqual(calls, ["a"])
        self.assertEqual(first, "a")
        self.assertIn("duplicate_tool_call", second)

    def test_guard_is_inactive_without_active_context(self) -> None:
        calls: list[str] = []

        @make_tool
        def echo(value: str) -> str:
            """Echo a value."""
            calls.append(value)
            return value

        guarded = make_guarded_tool(echo)

        self.assertEqual(guarded.invoke({"value": "a"}), "a")
        self.assertEqual(guarded.invoke({"value": "a"}), "a")
        self.assertEqual(calls, ["a", "a"])


if __name__ == "__main__":
    unittest.main()
