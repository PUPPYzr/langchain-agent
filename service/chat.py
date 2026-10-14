"""Derive honest UI events from real Runtime events.

The Runtime streams LangGraph state snapshots (``stream_mode="values"``). This
module turns those snapshots into small, display-safe events for the Web UI:

* tool calls and results come from the actual messages in the graph state;
* the answer comes from the actual ``run.completed`` payload.

Nothing is synthesized: if the Agent did not call a tool, no tool event is
emitted. The answer is *not* token-streamed, because the Runtime does not
produce token deltas — the UI renders it once complete instead of faking a
typing animation.

Internal reasoning is never projected; only message content and tool metadata
that the Agent itself produced.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from agent_platform.events import AgentEvent

MAX_ARGUMENT_STRING = 120
MAX_ARGUMENT_LENGTH = 8
MAX_ARGUMENTS_JSON = 600
MAX_RESULT_SUMMARY = 240

SENSITIVE_ARGUMENT_KEYS = (
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "access_token",
    "refresh_token",
)


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    if lowered == "key":
        return True
    return any(marker in lowered for marker in SENSITIVE_ARGUMENT_KEYS)


def _sanitize_value(value: Any, depth: int) -> Any:
    if depth > 2:
        return "…"
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_value(item, depth + 1)
            for key, item in list(value.items())[:MAX_ARGUMENT_LENGTH]
            if not _is_sensitive(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [
            _sanitize_value(item, depth + 1) for item in list(value)[:MAX_ARGUMENT_LENGTH]
        ]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    if len(text) > MAX_ARGUMENT_STRING:
        return text[:MAX_ARGUMENT_STRING] + "…"
    return text


def sanitize_arguments(arguments: Any) -> dict[str, Any]:
    """Return a display-safe, size-bounded view of tool arguments."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {"value": arguments}
    if not isinstance(arguments, Mapping):
        return {"value": _sanitize_value(arguments, 0)}
    safe = _sanitize_value(arguments, 0)
    encoded = json.dumps(safe, ensure_ascii=False, default=str)
    if len(encoded) > MAX_ARGUMENTS_JSON:
        return {"truncated": True}
    return safe


def _message_type(message: Any) -> str:
    if isinstance(message, Mapping):
        return str(message.get("type") or message.get("role") or "")
    return str(getattr(message, "type", "") or "")


def _message_field(message: Any, name: str, default: Any = None) -> Any:
    if isinstance(message, Mapping):
        return message.get(name, default)
    return getattr(message, name, default)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return "" if content is None else str(content)


def summarize_tool_result(content: Any) -> tuple[bool, str, str | None]:
    """Return ``(ok, summary, error_type)`` for a ToolMessage payload.

    Weather tools return structured JSON (``status``/``error_type``/``message``),
    so the UI can show a real failure instead of a generic error.
    """
    text = _text_of(content)
    error_type: str | None = None
    ok = True
    parsed: Any = None
    if text.strip().startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
    if isinstance(parsed, Mapping):
        status = str(parsed.get("status", "success"))
        if status == "error":
            ok = False
            raw_type = parsed.get("error_type")
            error_type = str(raw_type) if raw_type else None
        message = parsed.get("message")
        if isinstance(message, str) and message.strip():
            text = message.strip()
    collapsed = " ".join(text.split())
    if len(collapsed) > MAX_RESULT_SUMMARY:
        collapsed = collapsed[:MAX_RESULT_SUMMARY] + "…"
    return ok, collapsed, error_type


class ExecutionProjector:
    """Convert one Runtime event stream into display-safe UI events."""

    def __init__(self) -> None:
        self._seen_calls: set[str] = set()
        self._seen_results: set[str] = set()

    def project(self, event: AgentEvent) -> list[dict[str, Any]]:
        """Return the UI events derived from one Runtime event."""
        handler = {
            "run.started": self._on_run_started,
            "run.resumed": self._on_run_resumed,
            "state.updated": self._on_state_updated,
            "run.retry_scheduled": self._on_retry,
            "run.completed": self._on_run_completed,
            "run.interrupted": self._on_run_interrupted,
            "run.failed": self._on_run_failed,
            "run.cancelled": self._on_run_cancelled,
            "run.timeout": self._on_run_timeout,
        }.get(event.event_type)
        if handler is None:
            return []
        return handler(event.data)

    # --- run lifecycle -----------------------------------------------------

    @staticmethod
    def _on_run_started(_: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [{"type": "status", "phase": "started"}]

    @staticmethod
    def _on_run_resumed(_: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [{"type": "status", "phase": "resumed"}]

    @staticmethod
    def _on_retry(data: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "type": "status",
                "phase": "retrying",
                "attempt": data.get("attempt"),
                "error_category": data.get("error_category"),
            }
        ]

    @staticmethod
    def _on_run_completed(data: Mapping[str, Any]) -> list[dict[str, Any]]:
        content = data.get("content")
        events: list[dict[str, Any]] = []
        if isinstance(content, str) and content.strip():
            events.append({"type": "answer", "content": content})
        events.append({"type": "status", "phase": "completed"})
        return events

    @staticmethod
    def _on_run_interrupted(_: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [{"type": "status", "phase": "interrupted"}]

    @staticmethod
    def _on_run_failed(data: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "type": "error",
                "code": "run_failed",
                "message": "Agent 执行失败。",
                "error_category": data.get("error_type"),
            }
        ]

    @staticmethod
    def _on_run_cancelled(_: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [{"type": "status", "phase": "cancelled"}]

    @staticmethod
    def _on_run_timeout(_: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "type": "error",
                "code": "run_timeout",
                "message": "运行超时。",
                "error_category": "run_timeout",
            }
        ]

    # --- state derivation --------------------------------------------------

    def _on_state_updated(self, data: Mapping[str, Any]) -> list[dict[str, Any]]:
        state = data.get("state")
        if not isinstance(state, Mapping):
            return []
        messages = state.get("messages") or []
        events: list[dict[str, Any]] = []
        for message in messages:
            events.extend(self._project_message(message))
        if events:
            events.append({"type": "status", "phase": "working"})
        return events

    def _project_message(self, message: Any) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        tool_calls = _message_field(message, "tool_calls", None)
        if isinstance(tool_calls, (list, tuple)):
            for call in tool_calls:
                call_id = str(_message_field(call, "id", "") or "")
                name = str(_message_field(call, "name", "") or "tool")
                key = call_id or f"{name}:{len(self._seen_calls)}"
                if key in self._seen_calls:
                    continue
                self._seen_calls.add(key)
                events.append(
                    {
                        "type": "tool_call",
                        "id": key,
                        "name": name,
                        "arguments": sanitize_arguments(
                            _message_field(call, "args", {})
                        ),
                    }
                )
        if _message_type(message) == "tool":
            result_id = str(_message_field(message, "tool_call_id", "") or "")
            key = result_id or f"result:{len(self._seen_results)}"
            if key not in self._seen_results:
                self._seen_results.add(key)
                ok, summary, error_type = summarize_tool_result(
                    _message_field(message, "content", "")
                )
                events.append(
                    {
                        "type": "tool_result",
                        "id": key,
                        "name": str(_message_field(message, "name", "") or "tool"),
                        "ok": ok,
                        "summary": summary,
                        "error_type": error_type,
                    }
                )
        return events
