"""Framework-independent model configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """Versioned model configuration owned by the platform."""

    provider: str
    model: str
    temperature: float = 0.0
    timeout_seconds: float = 30.0
    max_retries: int = 0
    max_completion_tokens: int = 400
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ValueError("provider must not be empty")
        if not self.model.strip():
            raise ValueError("model must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")
        if self.max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if self.max_completion_tokens <= 0:
            raise ValueError("max_completion_tokens must be greater than 0")
