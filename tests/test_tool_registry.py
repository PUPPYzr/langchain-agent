import unittest

from agent_platform.tools import ToolRegistry, ToolSpec


class ToolRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.weather_tool = object()
        self.registry = ToolRegistry(
            [
                ToolSpec(
                    tool_id="get-current-weather",
                    version=1,
                    description="查询当前天气",
                    implementation=self.weather_tool,
                )
            ]
        )

    def test_resolves_registered_tool_by_id_and_version(self) -> None:
        registered = self.registry.resolve("get-current-weather", 1)

        self.assertIs(registered.implementation, self.weather_tool)
        self.assertEqual(registered.spec.permission, "read-only")

    def test_duplicate_registration_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.registry.register(
                ToolSpec(
                    tool_id="get-current-weather",
                    version=1,
                    description="重复工具",
                    implementation=object(),
                )
            )

    def test_missing_tool_is_rejected(self) -> None:
        with self.assertRaises(KeyError):
            self.registry.resolve("missing-tool", 1)

    def test_tool_spec_rejects_invalid_policy(self) -> None:
        with self.assertRaises(ValueError):
            ToolSpec(
                tool_id="bad-tool",
                version=0,
                description="无效工具",
                implementation=object(),
            )


if __name__ == "__main__":
    unittest.main()
