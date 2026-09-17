import unittest
from unittest.mock import patch

from agent_platform import AgentContext, AgentDefinition
from agent_platform.domain import AgentRuntimeError
from agent_platform.legacy_runtime import LegacyAgentRuntime
from weather_agent.agent import build_weather_agent
from weather_agent.agent_spec import build_weather_agent_spec
from weather_agent.config import Settings


class FakeAgent:
    def __init__(self, content: str = "天气结果") -> None:
        self.content = content
        self.received_config = None

    def invoke(self, payload: dict, *, config: dict | None = None) -> dict:
        self.received_config = config
        return {"messages": [type("Message", (), {"content": self.content})()]}


class AgentPlatformRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.definition = AgentDefinition(
            agent_id="weather-agent",
            version=1,
            goal="回答天气问题",
            runtime_type="legacy",
        )
        self.context = AgentContext(
            question="合肥天气怎么样？",
            run_id="run-1",
            session_id="session-1",
            metadata={"runtime_config": {"recursion_limit": 3}},
        )

    def test_legacy_runtime_returns_platform_result(self) -> None:
        runtime = LegacyAgentRuntime(lambda: FakeAgent())

        result = runtime.run(self.definition, self.context)

        self.assertEqual(result.content, "天气结果")
        self.assertEqual(result.run_id, "run-1")
        self.assertEqual(result.session_id, "session-1")
        self.assertEqual(result.metadata["runtime_type"], "legacy")

    def test_legacy_runtime_rejects_other_runtime_type(self) -> None:
        runtime = LegacyAgentRuntime(lambda: FakeAgent())
        definition = AgentDefinition(
            agent_id="weather-agent",
            version=1,
            goal="回答天气问题",
            runtime_type="langgraph",
        )

        with self.assertRaises(AgentRuntimeError):
            runtime.run(definition, self.context)

    def test_legacy_runtime_rejects_empty_question(self) -> None:
        runtime = LegacyAgentRuntime(lambda: FakeAgent())
        context = AgentContext(
            question=" ",
            run_id="run-1",
            session_id="session-1",
        )

        with self.assertRaises(AgentRuntimeError):
            runtime.run(self.definition, context)

    def test_weather_agent_factory_uses_platform_model_provider(self) -> None:
        class FakeModelProvider:
            def __init__(self) -> None:
                self.received_spec = None

            def create_chat_model(self, spec):
                self.received_spec = spec
                return "fake-model"

        provider = FakeModelProvider()
        settings = Settings(
            openai_api_key="test-key",
            openai_model="test-model",
            openai_base_url=None,
            model_temperature=0.0,
            model_timeout_seconds=30.0,
            model_max_retries=0,
            model_max_completion_tokens=400,
            agent_max_turns=4,
        )

        with patch("agent_platform.compiler.create_agent", return_value="fake-agent") as factory:
            result = build_weather_agent(settings, model_provider=provider)

        self.assertEqual(result, "fake-agent")
        self.assertEqual(provider.received_spec.model, "test-model")
        self.assertEqual(factory.call_args.kwargs["model"], "fake-model")
        self.assertEqual(len(factory.call_args.kwargs["tools"]), 3)
        self.assertIsNone(factory.call_args.kwargs["checkpointer"])

    def test_weather_agent_factory_builds_versioned_spec(self) -> None:
        settings = Settings(
            openai_api_key="test-key",
            openai_model="test-model",
            openai_base_url=None,
            model_temperature=0.0,
            model_timeout_seconds=30.0,
            model_max_retries=0,
            model_max_completion_tokens=400,
            agent_max_turns=4,
        )

        spec = build_weather_agent_spec(settings)

        self.assertEqual(spec.agent_id, "weather-agent")
        self.assertEqual(spec.version, 1)
        self.assertEqual(spec.prompt.prompt_id, "weather-assistant")
        self.assertEqual(spec.runtime_type, "legacy")

    def test_langgraph_agent_factory_defaults_to_checkpoint_provider(self) -> None:
        class FakeModelProvider:
            def create_chat_model(self, spec):
                return "fake-model"

        settings = Settings(
            openai_api_key="test-key",
            openai_model="test-model",
            openai_base_url=None,
            model_temperature=0.0,
            model_timeout_seconds=30.0,
            model_max_retries=0,
            model_max_completion_tokens=400,
            agent_max_turns=4,
            agent_runtime_type="langgraph",
        )

        with patch("agent_platform.compiler.create_agent", return_value="fake-agent") as factory:
            build_weather_agent(settings, model_provider=FakeModelProvider())

        self.assertIsNotNone(factory.call_args.kwargs["checkpointer"])


if __name__ == "__main__":
    unittest.main()
