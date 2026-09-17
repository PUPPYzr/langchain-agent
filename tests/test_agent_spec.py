import unittest

from agent_platform.models import ModelSpec
from agent_platform.spec import AgentSpec, PromptSpec, ToolReference


class AgentSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = ModelSpec(provider="openai-compatible", model="test-model")
        self.prompt = PromptSpec(
            prompt_id="weather-assistant",
            version=1,
            system="回答天气问题。",
        )

    def test_agent_spec_is_versioned_and_structured(self) -> None:
        spec = AgentSpec(
            agent_id="weather-agent",
            version=1,
            goal="回答天气问题",
            model=self.model,
            prompt=self.prompt,
            tools=(ToolReference("get-current-weather"),),
        )

        self.assertEqual(spec.agent_id, "weather-agent")
        self.assertEqual(spec.prompt.version, 1)
        self.assertEqual(spec.tools[0].tool_id, "get-current-weather")

    def test_empty_tool_set_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AgentSpec(
                agent_id="weather-agent",
                version=1,
                goal="回答天气问题",
                model=self.model,
                prompt=self.prompt,
                tools=(),
            )

    def test_invalid_prompt_and_tool_versions_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            PromptSpec(prompt_id="weather", version=0, system="天气")
        with self.assertRaises(ValueError):
            ToolReference("get-current-weather", version=0)


if __name__ == "__main__":
    unittest.main()
