"""LangChain implementation of the platform ModelProvider contract."""

from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI

from agent_platform.models.spec import ModelSpec


class LangChainModelProvider:
    """Adapt an OpenAI-compatible model to the platform model contract."""

    def __init__(self, *, api_key: str, base_url: str | None = None) -> None:
        if not api_key.strip():
            raise ValueError("api_key must not be empty")
        self._api_key = api_key
        self._base_url = base_url

    def create_chat_model(self, spec: ModelSpec) -> Any:
        if spec.provider != "openai-compatible":
            raise ValueError(
                "LangChainModelProvider only supports the "
                "openai-compatible provider"
            )

        options: dict[str, Any] = {
            "model": spec.model,
            "api_key": self._api_key,
            "temperature": spec.temperature,
            "timeout": spec.timeout_seconds,
            "max_retries": spec.max_retries,
            "max_completion_tokens": spec.max_completion_tokens,
        }
        if self._base_url:
            options["base_url"] = self._base_url
        return ChatOpenAI(**options)
