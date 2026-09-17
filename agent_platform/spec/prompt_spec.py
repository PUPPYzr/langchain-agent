"""Versioned prompt definition owned by the platform."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True, slots=True)
class PromptSpec:
    """A named, versioned system prompt."""

    prompt_id: str
    version: int
    system: str
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.prompt_id.strip():
            raise ValueError("prompt_id must not be empty")
        if self.version < 1:
            raise ValueError("version must be at least 1")
        if not self.system.strip():
            raise ValueError("system prompt must not be empty")
