import unittest
from unittest.mock import patch

from agent_platform.models import LangChainModelProvider, ModelSpec


class ModelSpecTests(unittest.TestCase):
    def test_model_spec_validates_runtime_limits(self) -> None:
        with self.assertRaises(ValueError):
            ModelSpec(provider="openai-compatible", model="gpt", timeout_seconds=0)
        with self.assertRaises(ValueError):
            ModelSpec(provider="openai-compatible", model="gpt", max_retries=-1)

    def test_langchain_provider_maps_platform_spec(self) -> None:
        spec = ModelSpec(
            provider="openai-compatible",
            model="gpt-test",
            temperature=0.2,
            timeout_seconds=12,
            max_retries=1,
            max_completion_tokens=128,
        )
        with patch(
            "agent_platform.models.langchain_provider.ChatOpenAI",
            return_value="fake-model",
        ) as chat_model:
            result = LangChainModelProvider(
                api_key="test-key",
                base_url="https://example.test/v1",
            ).create_chat_model(spec)

        self.assertEqual(result, "fake-model")
        chat_model.assert_called_once_with(
            model="gpt-test",
            api_key="test-key",
            temperature=0.2,
            timeout=12,
            max_retries=1,
            max_completion_tokens=128,
            base_url="https://example.test/v1",
        )

    def test_provider_rejects_unknown_provider(self) -> None:
        spec = ModelSpec(provider="other", model="model")
        provider = LangChainModelProvider(api_key="test-key")

        with self.assertRaises(ValueError):
            provider.create_chat_model(spec)


if __name__ == "__main__":
    unittest.main()
