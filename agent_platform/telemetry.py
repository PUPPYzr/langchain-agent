"""Platform telemetry contracts: error categories, redaction and tracing policy.

Everything that writes a trace (platform runtime, CLI callbacks, weather tools)
uses the same stage names, the same error classification and the same redaction
rules, so a trace is comparable across producers and never leaks credentials.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Iterator

from agent_platform.domain import (
    AgentRunCancelledError,
    AgentRunTimeoutError,
    ResumeNotAllowedError,
)

# --- canonical trace stages -------------------------------------------------

STAGE_RUN = "run"
STAGE_RUNTIME = "runtime"
STAGE_MODEL = "model"
STAGE_TOOL = "tool"
STAGE_HTTP = "http"
STAGE_RETRY = "retry"

CANONICAL_STAGES: tuple[str, ...] = (
    STAGE_RUN,
    STAGE_RUNTIME,
    STAGE_MODEL,
    STAGE_TOOL,
    STAGE_HTTP,
    STAGE_RETRY,
)

# --- unified error categories ----------------------------------------------

CATEGORY_CANCELLED = "cancelled"
CATEGORY_RUN_TIMEOUT = "run_timeout"
CATEGORY_MODEL_TIMEOUT = "model_timeout"
CATEGORY_RATE_LIMIT = "rate_limit"
CATEGORY_AUTHENTICATION = "authentication_error"
CATEGORY_TURN_LIMIT = "agent_turn_limit"
CATEGORY_RESUME_NOT_ALLOWED = "resume_not_allowed"
CATEGORY_AGENT_ERROR = "agent_error"

ERROR_CATEGORIES: tuple[str, ...] = (
    CATEGORY_CANCELLED,
    CATEGORY_RUN_TIMEOUT,
    CATEGORY_MODEL_TIMEOUT,
    CATEGORY_RATE_LIMIT,
    CATEGORY_AUTHENTICATION,
    CATEGORY_TURN_LIMIT,
    CATEGORY_RESUME_NOT_ALLOWED,
    CATEGORY_AGENT_ERROR,
)

_MAX_CAUSE_DEPTH = 20


def _cause_chain(error: BaseException) -> Iterator[BaseException]:
    """Yield the error and its causes without looping on cyclic chains."""
    seen: set[int] = set()
    current: BaseException | None = error
    depth = 0
    while current is not None and depth < _MAX_CAUSE_DEPTH:
        if id(current) in seen:
            return
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__
        depth += 1


def classify_error(error: BaseException) -> str:
    """Map any failure to one stable platform error category.

    Platform exceptions are authoritative and win over provider heuristics; the
    remaining categories fall back to well-known message markers so that a
    provider SDK upgrade cannot silently change the reported category.
    """
    chain = list(_cause_chain(error))
    for candidate in chain:
        if isinstance(candidate, AgentRunCancelledError):
            return CATEGORY_CANCELLED
    for candidate in chain:
        if isinstance(candidate, AgentRunTimeoutError):
            return CATEGORY_RUN_TIMEOUT
    for candidate in chain:
        if isinstance(candidate, ResumeNotAllowedError):
            return CATEGORY_RESUME_NOT_ALLOWED
    text = " | ".join(f"{type(item).__name__}: {item}" for item in chain).lower()
    if "recursion" in text:
        return CATEGORY_TURN_LIMIT
    if "ratelimit" in text or "rate_limit" in text or "429" in text:
        return CATEGORY_RATE_LIMIT
    if (
        "authentication" in text
        or "permission denied" in text
        or "401" in text
        or "403" in text
    ):
        return CATEGORY_AUTHENTICATION
    if "timeout" in text or "timed out" in text:
        return CATEGORY_MODEL_TIMEOUT
    return CATEGORY_AGENT_ERROR


# --- redaction --------------------------------------------------------------

REDACTED = "<redacted>"

# Exact key names that always carry credentials.
SENSITIVE_KEYS = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "api_token",
        "authorization",
        "password",
        "secret",
        "key",
    }
)

# Substrings that mark a credential-bearing key name.
SENSITIVE_SUBSTRINGS = ("authorization", "api_key", "apikey", "secret", "password")

# Keys that carry user content and are hidden unless content capture is on.
CONTENT_KEYS = frozenset(
    {
        "question",
        "query",
        "content",
        "message",
        "final_answer",
        "arguments",
        "tool_input",
        "prompt",
    }
)


@dataclass(frozen=True, slots=True)
class TracingPolicy:
    """How much of a trace may be persisted, and for how long."""

    capture_content: bool = False
    max_string_length: int = 200
    retention_days: int = 7

    def __post_init__(self) -> None:
        if self.max_string_length < 16:
            raise ValueError("max_string_length must be at least 16")
        if self.retention_days < 0:
            raise ValueError("retention_days must not be negative")

    @classmethod
    def from_environment(cls) -> "TracingPolicy":
        """Load the policy from the environment with safe defaults."""
        raw_capture = os.getenv("TRACE_CAPTURE_CONTENT", "false").strip().lower()
        try:
            max_string_length = int(os.getenv("TRACE_MAX_STRING_LENGTH", "200").strip())
            retention_days = int(os.getenv("TRACE_RETENTION_DAYS", "7").strip())
        except ValueError as exc:
            raise ValueError(
                "TRACE_MAX_STRING_LENGTH and TRACE_RETENTION_DAYS must be integers."
            ) from exc
        return cls(
            capture_content=raw_capture in {"1", "true", "yes", "on"},
            max_string_length=max_string_length,
            retention_days=retention_days,
        )


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in SENSITIVE_KEYS:
        return True
    return any(marker in lowered for marker in SENSITIVE_SUBSTRINGS)


def redact(value: Any, *, policy: TracingPolicy, key: str | None = None) -> Any:
    """Return a trace-safe copy of a detail value.

    Credentials are always removed. User content is removed unless the policy
    captures it. Long strings are truncated so a single event cannot bloat the
    trace file.
    """
    if key is not None:
        if _is_sensitive_key(key):
            return REDACTED
        if not policy.capture_content and key.lower() in CONTENT_KEYS:
            return REDACTED
    if isinstance(value, Mapping):
        return {
            str(item_key): redact(item_value, policy=policy, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, policy=policy) for item in value]
    if isinstance(value, str) and len(value) > policy.max_string_length:
        return value[: policy.max_string_length] + "…"
    return value


def purge_expired_traces(
    directory: str | Path,
    policy: TracingPolicy,
    *,
    now: datetime | None = None,
) -> int:
    """Delete trace files older than the retention window; return the count."""
    if policy.retention_days == 0:
        return 0
    trace_directory = Path(directory)
    if not trace_directory.exists():
        return 0
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(
        days=policy.retention_days
    )
    removed = 0
    for path in trace_directory.glob("*.jsonl"):
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if modified < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def iter_trace_records(path: str | Path) -> Iterable[Mapping[str, Any]]:
    """Yield parsed JSONL records, ignoring blank and unparsable lines."""
    import json

    file_path = Path(path)
    if not file_path.exists():
        return
    for line in file_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping):
            yield payload
