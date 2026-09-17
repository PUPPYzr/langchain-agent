"""Framework-independent Tool specification."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Versioned metadata and execution policy for one platform Tool."""

    tool_id: str
    version: int
    description: str
    implementation: Any
    tool_type: str = "builtin"
    permission: Literal[
        "read-only",
        "reversible-write",
        "external-side-effect",
        "high-risk",
    ] = "read-only"
    timeout_seconds: float = 15.0
    max_retries: int = 0
    input_schema: Any | None = None
    output_schema: Any | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.tool_id.strip():
            raise ValueError("tool_id must not be empty")
        if self.version < 1:
            raise ValueError("version must be at least 1")
        if not self.description.strip():
            raise ValueError("description must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")
        if self.max_retries < 0:
            raise ValueError("max_retries must not be negative")
