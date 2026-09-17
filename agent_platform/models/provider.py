"""Platform model provider protocol."""

from __future__ import annotations

from typing import Any, Protocol

from agent_platform.models.spec import ModelSpec


class ModelProvider(Protocol):
    """Create a chat model without exposing provider SDK details to callers."""

    def create_chat_model(self, spec: ModelSpec) -> Any:
        """Build a model instance from a platform-owned ModelSpec."""
