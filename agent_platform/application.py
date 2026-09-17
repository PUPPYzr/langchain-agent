"""Application service for Agent execution lifecycle orchestration."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from time import perf_counter
from threading import Event

from agent_platform.compiler import CompiledAgent
from agent_platform.checkpoints import CheckpointRecord, CheckpointRepository
from agent_platform.domain import (
    AgentContext,
    AgentRunCancelledError,
    AgentRunResult,
    AgentRunTimeoutError,
)
from agent_platform.events import AgentEvent
from agent_platform.runtime import RuntimeRegistry
from agent_platform.runs import InMemoryRunRepository, RunRecord, RunRepository


class AgentApplicationService:
    """Select a Runtime and maintain the platform Run lifecycle."""

    def __init__(
        self,
        *,
        runtime_registry: RuntimeRegistry | None = None,
        run_repository: RunRepository | None = None,
        checkpoint_repository: CheckpointRepository | None = None,
    ) -> None:
        self._runtime_registry = runtime_registry or RuntimeRegistry.with_defaults()
        self._run_repository = run_repository or InMemoryRunRepository()
        self._checkpoint_repository = checkpoint_repository

    def run(
        self,
        compiled: CompiledAgent,
        context: AgentContext,
    ) -> AgentRunResult:
        definition = compiled.spec.to_definition()
        record = RunRecord.pending(definition, context).transition("running")
        self._run_repository.save(record)
        runtime = self._runtime_registry.create(
            compiled.spec.runtime_type,
            lambda: compiled.executable,
        )
        try:
            result = self._execute_with_policy(
                lambda: runtime.run(definition, context),
                timeout_seconds=compiled.spec.timeout_seconds,
                cancel_event=context.metadata.get("cancel_event"),
                max_retries=int(context.metadata.get("max_retries", 0)),
            )
        except Exception as exc:
            self._run_repository.save(
                record.transition(
                    "cancelled" if isinstance(exc, AgentRunCancelledError) else "failed",
                    error_type=type(exc).__name__,
                )
            )
            raise
        self._run_repository.save(record.transition("completed", result=result))
        return result

    @staticmethod
    def _execute_with_policy(
        operation,
        *,
        timeout_seconds: float,
        cancel_event: Event | None,
        max_retries: int,
    ) -> AgentRunResult:
        if cancel_event is not None and cancel_event.is_set():
            raise AgentRunCancelledError("Agent run was cancelled before execution.")
        attempts = max(0, max_retries) + 1
        last_error: Exception | None = None
        for _ in range(attempts):
            executor = ThreadPoolExecutor(max_workers=1)
            future = executor.submit(operation)
            try:
                result = future.result(timeout=timeout_seconds)
                executor.shutdown(wait=True)
                return result
            except FutureTimeoutError as exc:
                future.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                raise AgentRunTimeoutError(
                    f"Agent run exceeded {timeout_seconds:.1f}s timeout."
                ) from exc
            except AgentRunCancelledError:
                executor.shutdown(wait=False, cancel_futures=True)
                raise
            except Exception as exc:
                executor.shutdown(wait=True)
                last_error = exc
                if cancel_event is not None and cancel_event.is_set():
                    raise AgentRunCancelledError("Agent run was cancelled.") from exc
        assert last_error is not None
        raise last_error

    def stream(
        self,
        compiled: CompiledAgent,
        context: AgentContext,
    ) -> Iterator[AgentEvent]:
        definition = compiled.spec.to_definition()
        record = RunRecord.pending(definition, context).transition("running")
        self._run_repository.save(record)
        runtime = self._runtime_registry.create(
            compiled.spec.runtime_type,
            lambda: compiled.executable,
        )
        completed_result: AgentRunResult | None = None
        timeout_seconds = compiled.spec.timeout_seconds
        cancel_event = context.metadata.get("cancel_event")
        max_retries = max(0, int(context.metadata.get("max_retries", 0)))
        try:
            started_at = perf_counter()
            for attempt in range(max_retries + 1):
                try:
                    if cancel_event is not None and cancel_event.is_set():
                        raise AgentRunCancelledError("Agent run was cancelled.")
                    for event in runtime.stream(definition, context):
                        if cancel_event is not None and cancel_event.is_set():
                            raise AgentRunCancelledError("Agent run was cancelled.")
                        if perf_counter() - started_at > timeout_seconds:
                            raise AgentRunTimeoutError(
                                f"Agent run exceeded {timeout_seconds:.1f}s timeout."
                            )
                        if event.event_type == "run.completed":
                            completed_result = AgentRunResult(
                                run_id=context.run_id,
                                session_id=context.session_id,
                                content=str(event.data.get("content", "")),
                                metadata={"runtime_type": compiled.spec.runtime_type},
                            )
                        yield event
                    break
                except (AgentRunCancelledError, AgentRunTimeoutError):
                    raise
                except Exception:
                    if attempt >= max_retries:
                        raise
        except Exception as exc:
            self._run_repository.save(
                record.transition(
                    "cancelled" if isinstance(exc, AgentRunCancelledError) else "failed",
                    error_type=type(exc).__name__,
                )
            )
            raise
        self._run_repository.save(
            record.transition("completed", result=completed_result)
        )

    def get_run(self, run_id: str) -> RunRecord:
        return self._run_repository.get(run_id)

    def list_runs(self, *, session_id: str | None = None, status=None, limit: int | None = None) -> tuple[RunRecord, ...]:
        return self._run_repository.list(session_id=session_id, status=status, limit=limit)

    def list_threads(self, *, limit: int | None = None):
        return self._run_repository.list_threads(limit=limit)

    def list_checkpoints(self, thread_id: str | None = None, *, limit: int | None = None) -> tuple[CheckpointRecord, ...]:
        if self._checkpoint_repository is None:
            raise RuntimeError("Checkpoint repository is not configured")
        return self._checkpoint_repository.list(thread_id, limit=limit)

    def resume(
        self,
        compiled: CompiledAgent,
        context: AgentContext,
    ) -> AgentRunResult:
        """Resume a checkpointed run through the selected Runtime."""
        definition = compiled.spec.to_definition()
        record = RunRecord.pending(definition, context).transition("running")
        self._run_repository.save(record)
        runtime = self._runtime_registry.create(
            compiled.spec.runtime_type,
            lambda: compiled.executable,
        )
        try:
            result = runtime.resume(definition, context)
        except Exception as exc:
            self._run_repository.save(
                record.transition(
                    "cancelled" if isinstance(exc, AgentRunCancelledError) else "failed",
                    error_type=type(exc).__name__,
                )
            )
            raise
        self._run_repository.save(record.transition("completed", result=result))
        return result
