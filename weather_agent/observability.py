"""Runtime progress display and structured trace persistence."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from threading import Lock
from time import perf_counter
from typing import Any, Iterator

TRACE_DIRECTORY = Path(__file__).resolve().parent.parent / "observability" / "traces"
_active_recorder: ContextVar["TraceRecorder | None"] = ContextVar(
    "active_trace_recorder",
    default=None,
)
_active_progress: ContextVar[ProgressReporter | None] = ContextVar(
    "active_progress_reporter",
    default=None,
)


class ProgressReporter:
    """Render one compact, cumulative progress line on stderr."""

    def __init__(self) -> None:
        self._started_at = perf_counter()
        self._last_stage = "准备中"
        self._last_line_length = 0
        self._lock = Lock()

    def update(self, stage: str) -> None:
        with self._lock:
            self._last_stage = stage
            elapsed = perf_counter() - self._started_at
            line = f"[进度] {stage} | 已用时 {elapsed:.1f} 秒"
            if not sys.stderr.isatty():
                sys.stderr.write(line + "\n")
                sys.stderr.flush()
                return
            sys.stderr.write("\x1b[2K\r" + line)
            sys.stderr.flush()
            self._last_line_length = len(line)

    def finish(self, stage: str = "完成") -> None:
        with self._lock:
            self._last_stage = stage
            elapsed = perf_counter() - self._started_at
            line = f"[进度] {stage} | 已用时 {elapsed:.1f} 秒"
            if not sys.stderr.isatty():
                sys.stderr.write(line + "\n")
            else:
                sys.stderr.write("\x1b[2K\r" + line + "\n")
            sys.stderr.flush()
            self._last_line_length = 0


@contextmanager
def progress_context(progress: ProgressReporter) -> Iterator[ProgressReporter]:
    """Make one cumulative progress reporter available to nested services."""
    token = _active_progress.set(progress)
    try:
        yield progress
    finally:
        _active_progress.reset(token)


def report_progress(stage: str) -> None:
    """Update the active progress reporter when one exists."""
    progress = _active_progress.get()
    if progress is not None:
        progress.update(stage)


class TraceRecorder:
    """Persist one append-only JSONL trace file for one run."""

    def __init__(self, *, run_id: str, session_id: str, model_name: str | None = None) -> None:
        self.run_id = run_id
        self.session_id = session_id
        self.model_name = model_name
        self.started_at = perf_counter()
        TRACE_DIRECTORY.mkdir(parents=True, exist_ok=True)
        self.path = TRACE_DIRECTORY / f"{run_id}.jsonl"
        self._file = self.path.open("a", encoding="utf-8")
        self._terminal_event_recorded = False

    def record(self, stage: str, event: str, **details: Any) -> None:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(perf_counter() - self.started_at, 3),
            "run_id": self.run_id,
            "session_id": self.session_id,
            "model": self.model_name,
            "stage": stage,
            "event": event,
            "details": details,
        }
        self._file.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        self._file.flush()
        if stage == "run" and event in {"failed", "finished"}:
            self._terminal_event_recorded = True

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()

    def __enter__(self) -> "TraceRecorder":
        self._token = _active_recorder.set(self)
        self.record("run", "started")
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        if exc_value is not None:
            self.record("run", "failed", error_type=type(exc_value).__name__)
        elif not self._terminal_event_recorded:
            self.record("run", "finished")
        _active_recorder.reset(self._token)
        self.close()


def record_trace(stage: str, event: str, **details: Any) -> None:
    """Record an event when a run context is active."""
    recorder = _active_recorder.get()
    if recorder is not None:
        recorder.record(stage, event, **details)
