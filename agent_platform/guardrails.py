"""Platform tool-call policy enforced inside Agent execution.

A callback that raises cannot stop a tool call: LangGraph's ``ToolNode``
catches tool (and tool-callback) errors and turns them into a tool message, so
the run continues. The reliable enforcement point is the tool itself, so the
Compiler wraps every registered tool and each call passes through the guard that
is active for the current run.

Per-run state travels in a ``ContextVar``. ``AgentApplicationService`` copies the
caller context into the worker thread, and the Runtime binds a fresh guard for
each run, so concurrent or sequential runs never share guard state.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Mapping

from langchain_core.tools import BaseTool, StructuredTool

from agent_platform.cancellation import current_cancellation_token

DUPLICATE_TOOL_CALL = "duplicate_tool_call"
TOOL_CALL_BUDGET_EXHAUSTED = "tool_call_budget_exhausted"
CANCELLED = "cancelled"


class ToolCallGuard:
    """Per-run tool-call policy: block identical repeats and enforce a budget."""

    def __init__(self, *, max_tool_calls: int | None = None) -> None:
        if max_tool_calls is not None and max_tool_calls < 1:
            raise ValueError("max_tool_calls must be >= 1")
        self._max_tool_calls = max_tool_calls
        self._seen: set[tuple[str, str]] = set()
        self.tool_call_count = 0
        self.blocked_duplicates = 0
        self.blocked_over_budget = 0
        self.blocked_cancelled = 0

    def check(self, tool_name: str, arguments: Mapping[str, Any]) -> str | None:
        """Return a structured error payload when a call must be blocked."""
        token = current_cancellation_token()
        if token is not None and token.cancelled:
            self.blocked_cancelled += 1
            return self._error(
                CANCELLED,
                "运行已取消，请立即基于已有信息作答，不要再调用工具。",
            )
        normalized = json.dumps(
            dict(arguments),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        key = (tool_name, normalized)
        if key in self._seen:
            self.blocked_duplicates += 1
            return self._error(
                DUPLICATE_TOOL_CALL,
                f"已阻止重复调用 {tool_name}（参数完全相同）。"
                "请勿再次调用，直接基于已有结果作答。",
            )
        if (
            self._max_tool_calls is not None
            and self.tool_call_count >= self._max_tool_calls
        ):
            self.blocked_over_budget += 1
            return self._error(
                TOOL_CALL_BUDGET_EXHAUSTED,
                f"工具调用预算已用尽（上限 {self._max_tool_calls}）。"
                "请直接基于已有结果作答。",
            )
        self._seen.add(key)
        self.tool_call_count += 1
        return None

    def summary(self) -> dict[str, int]:
        """Return counters suitable for Run trace and metadata."""
        return {
            "tool_call_count": self.tool_call_count,
            "blocked_duplicates": self.blocked_duplicates,
            "blocked_over_budget": self.blocked_over_budget,
            "blocked_cancelled": self.blocked_cancelled,
        }

    @staticmethod
    def _error(error_type: str, message: str) -> str:
        return json.dumps(
            {
                "status": "error",
                "error_type": error_type,
                "message": message,
                "retryable": False,
            },
            ensure_ascii=False,
        )


_active_guard: ContextVar[ToolCallGuard | None] = ContextVar(
    "active_tool_call_guard",
    default=None,
)


@contextmanager
def active_tool_guard(guard: ToolCallGuard) -> Iterator[ToolCallGuard]:
    """Bind one guard to the current execution context."""
    token = _active_guard.set(guard)
    try:
        yield guard
    finally:
        _active_guard.reset(token)


def current_tool_guard() -> ToolCallGuard | None:
    """Return the guard bound to the current execution context, if any."""
    return _active_guard.get()


def make_guarded_tool(tool: Any) -> Any:
    """Wrap a LangChain tool so every call passes through the active guard.

    Non-tool implementations are returned unchanged, which keeps the Compiler
    usable with plain callables in tests and non-LangChain providers.
    """
    if not isinstance(tool, BaseTool):
        return tool
    original = getattr(tool, "func", None)
    if original is None:
        return tool
    name = tool.name
    description = tool.description
    args_schema = tool.args_schema

    def guarded(**kwargs: Any) -> Any:
        guard = current_tool_guard()
        if guard is not None:
            blocked = guard.check(name, kwargs)
            if blocked is not None:
                return blocked
        return original(**kwargs)

    return StructuredTool.from_function(
        func=guarded,
        name=name,
        description=description,
        args_schema=args_schema,
    )
