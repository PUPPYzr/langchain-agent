import unittest

from agent_platform.models import ModelSpec
from agent_platform.spec import (
    AgentSpec,
    AgentSpecValidationError,
    PromptSpec,
    ToolReference,
    validate_agent_spec,
)
from weather_agent.tool_registry import WEATHER_TOOL_REGISTRY


class AgentSpecValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = AgentSpec(
            agent_id="weather-agent",
            version=1,
            goal="回答天气问题",
            model=ModelSpec(
                provider="openai-compatible",
                model="gpt-test",
                timeout_seconds=30,
            ),
            prompt=PromptSpec(
                prompt_id="weather-assistant",
                version=1,
                system="回答天气问题。",
            ),
            tools=(
                ToolReference("get-current-weather"),
                ToolReference("get-weather-forecast"),
                ToolReference("compare-current-weather"),
            ),
        )

    def test_validates_registered_weather_agent(self) -> None:
        self.assertIs(validate_agent_spec(self.spec, WEATHER_TOOL_REGISTRY), self.spec)

    def test_missing_tool_is_rejected_before_runtime(self) -> None:
        invalid = AgentSpec(
            agent_id=self.spec.agent_id,
            version=self.spec.version,
            goal=self.spec.goal,
            model=self.spec.model,
            prompt=self.spec.prompt,
            tools=(ToolReference("missing-tool"),),
        )

        with self.assertRaises(AgentSpecValidationError) as context:
            validate_agent_spec(invalid, WEATHER_TOOL_REGISTRY)

        self.assertEqual(context.exception.field, "tools")

    def test_unknown_runtime_is_rejected(self) -> None:
        invalid = AgentSpec(
            agent_id=self.spec.agent_id,
            version=self.spec.version,
            goal=self.spec.goal,
            model=self.spec.model,
            prompt=self.spec.prompt,
            tools=self.spec.tools,
            runtime_type="custom-runtime",
        )

        with self.assertRaises(AgentSpecValidationError) as context:
            validate_agent_spec(invalid, WEATHER_TOOL_REGISTRY)

        self.assertEqual(context.exception.field, "runtime_type")

    def test_agent_timeout_cannot_be_shorter_than_model_timeout(self) -> None:
        invalid = AgentSpec(
            agent_id=self.spec.agent_id,
            version=self.spec.version,
            goal=self.spec.goal,
            model=self.spec.model,
            prompt=self.spec.prompt,
            tools=self.spec.tools,
            timeout_seconds=10,
        )

        with self.assertRaises(AgentSpecValidationError) as context:
            validate_agent_spec(invalid, WEATHER_TOOL_REGISTRY)

        self.assertEqual(context.exception.field, "timeout_seconds")


if __name__ == "__main__":
    unittest.main()
