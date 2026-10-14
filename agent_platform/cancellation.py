"""Cooperative cancellation contract for Agent runs.

Python cannot interrupt a blocking socket read or a synchronous model call from
another thread. Cancellation is therefore cooperative: one token is shared with
the Run and every checkpoint raises ``AgentRunCancelledError`` once the token is
set.

Enforcement points:

* before execution starts;
* before each tool call (the platform ``ToolCallGuard`` consults the token);
* between streamed events.

A cancelled or timed-out Run is recorded as ``cancelled``/``failed`` — never as
``completed`` — and an in-flight model or HTTP call is allowed to finish before
its result is discarded. True forced termination of blocking I/O requires an
async Runtime (cancellable awaits) or process isolation; both are documented in
``docs/ARCHITECTURE.md`` and intentionally not adopted yet.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event
from typing import Any, Iterator

from agent_platform.domain import AgentRunCancelledError


class CancellationToken:
    """Thread-safe cooperative cancellation signal for one Run."""

    def __init__(self, event: Event | None = None) -> None:
        self._event = event if event is not None else Event()
        self._reason: str | None = None

    @property
    def cancelled(self) -> bool:
        """Return whether cancellation has been requested."""
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        """Return why cancellation was requested, when known."""
        return self._reason

    def request_cancel(self, reason: str | None = None) -> None:
        """Request cancellation; safe to call from any thread."""
        if reason is not None and self._reason is None:
            self._reason = reason
        self._event.set()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until cancellation is requested or the timeout expires."""
        return self._event.wait(timeout)

    def raise_if_cancelled(self, stage: str) -> None:
        """Raise when cancellation was requested before ``stage``."""
        if self.cancelled:
            detail = f": {self._reason}" if self._reason else ""
            raise AgentRunCancelledError(
                f"Agent run was cancelled before {stage}{detail}."
            )


_active_token: ContextVar[CancellationToken | None] = ContextVar(
    "active_cancellation_token",
    default=None,
)


@contextmanager
def cancellation_scope(token: CancellationToken) -> Iterator[CancellationToken]:
    """Bind a token to the current context.

    ``AgentApplicationService`` copies the caller context into the worker thread,
    so the guard and tool wrappers running there observe the same token.
    """
    context_token = _active_token.set(token)
    try:
        yield token
    finally:
        _active_token.reset(context_token)


def current_cancellation_token() -> CancellationToken | None:
    """Return the token bound to the current execution context, if any."""
    return _active_token.get()


def resolve_cancellation_token(candidate: Any) -> CancellationToken:
    """Adapt an existing token or a legacy ``threading.Event`` into a token."""
    if isinstance(candidate, CancellationToken):
        return candidate
    if candidate is not None and hasattr(candidate, "is_set") and hasattr(
        candidate, "set"
    ):
        return CancellationToken(candidate)
    return CancellationToken()
