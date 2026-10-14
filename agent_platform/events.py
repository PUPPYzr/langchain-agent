"""Framework-independent runtime events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

from agent_platform.domain import AgentContext


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """One normalized event emitted during an Agent run."""

    event_type: str
    run_id: str
    session_id: str
    data: Mapping[str, Any] = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class EventSink(Protocol):
    """Consume normalized Agent events."""

    def publish(self, event: AgentEvent) -> None:
        """Publish one event."""


class InMemoryEventSink:
    """Small event sink used by local runtimes and tests."""

    def __init__(self) -> None:
        self._events: list[AgentEvent] = []

    def publish(self, event: AgentEvent) -> None:
        self._events.append(event)

    @property
    def events(self) -> tuple[AgentEvent, ...]:
        return tuple(self._events)


class CallbackEventSink:
    """Adapt a callback such as a trace recorder to the EventSink contract."""

    def __init__(self, callback: Any) -> None:
        if not callable(callback):
            raise TypeError("callback must be callable")
        self._callback = callback

    def publish(self, event: AgentEvent) -> None:
        self._callback(
            event.event_type,
            run_id=event.run_id,
            session_id=event.session_id,
            **dict(event.data),
        )


def emit_event(context: AgentContext, event_type: str, **data: Any) -> None:
    """Publish one platform event to the Run's sink when one is configured.

    Every producer (runtime, application service, tool guard) uses this helper so
    that events always carry the same ``run_id``/``session_id`` correlation and
    land in the same trace as the Run they belong to.
    """
    sink = context.metadata.get("event_sink")
    if not hasattr(sink, "publish"):
        return
    sink.publish(
        AgentEvent(
            event_type=event_type,
            run_id=context.run_id,
            session_id=context.session_id,
            data=data,
        )
    )
