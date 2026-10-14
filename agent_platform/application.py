"""Application service for Agent execution lifecycle orchestration."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextvars import copy_context
from time import perf_counter
from threading import Event

from agent_platform.cancellation import cancellation_scope, resolve_cancellation_token
from agent_platform.compiler import CompiledAgent
from agent_platform.checkpoints import CheckpointRecord, CheckpointRepository
from agent_platform.domain import (
    AgentContext,
    AgentRunCancelledError,
    AgentRunResult,
    AgentRunTimeoutError,
    ResumeNotAllowedError,
)
from agent_platform.events import AgentEvent, emit_event
from agent_platform.telemetry import (
    CATEGORY_CANCELLED,
    CATEGORY_RUN_TIMEOUT,
    classify_error,
)
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
                context=context,
            )
        except KeyboardInterrupt:
            emit_event(
                context,
                "run.cancelled",
                error_category=CATEGORY_CANCELLED,
                reason="keyboard_interrupt",
            )
            self._run_repository.save(
                record.transition("cancelled", error_type=CATEGORY_CANCELLED)
            )
            raise
        except Exception as exc:
            category = classify_error(exc)
            if isinstance(exc, AgentRunCancelledError):
                emit_event(context, "run.cancelled", error_category=category)
            elif isinstance(exc, AgentRunTimeoutError):
                emit_event(context, "run.timeout", error_category=category)
            self._run_repository.save(
                record.transition(
                    "cancelled" if isinstance(exc, AgentRunCancelledError) else "failed",
                    error_type=category,
                )
            )
            raise
        self._run_repository.save(record.transition(result.status, result=result))
        return result

    @staticmethod
    def _execute_with_policy(
        operation,
        *,
        timeout_seconds: float,
        cancel_event: Event | None,
        max_retries: int,
        context: AgentContext | None = None,
    ) -> AgentRunResult:
        token = resolve_cancellation_token(cancel_event)
        token.raise_if_cancelled("execution")
        attempts = max(0, max_retries) + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            executor = ThreadPoolExecutor(max_workers=1)
            # ContextVars (trace recorder, progress reporter, cancellation
            # token) are not inherited by new threads. Copy the caller context
            # so callbacks and tool guards running in the worker thread observe
            # the same trace, progress and cancellation state.
            with cancellation_scope(token):
                worker_context = copy_context()
                future = executor.submit(worker_context.run, operation)
            try:
                result = future.result(timeout=timeout_seconds)
                executor.shutdown(wait=True)
                if token.cancelled:
                    token.raise_if_cancelled("completion")
                return result
            except FutureTimeoutError as exc:
                token.request_cancel("run timeout")
                future.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                if context is not None:
                    emit_event(
                        context,
                        "run.timeout",
                        error_category=CATEGORY_RUN_TIMEOUT,
                        attempt=attempt + 1,
                        timeout_seconds=timeout_seconds,
                    )
                raise AgentRunTimeoutError(
                    f"Agent run exceeded {timeout_seconds:.1f}s timeout. "
                    "In-flight model or HTTP calls cannot be interrupted and "
                    "may still be draining."
                ) from exc
            except AgentRunCancelledError:
                token.request_cancel("cancelled")
                executor.shutdown(wait=False, cancel_futures=True)
                if context is not None:
                    emit_event(
                        context,
                        "run.cancelled",
                        error_category=CATEGORY_CANCELLED,
                        attempt=attempt + 1,
                    )
                raise
            except Exception as exc:
                executor.shutdown(wait=True)
                last_error = exc
                if token.cancelled:
                    if context is not None:
                        emit_event(
                            context,
                            "run.cancelled",
                            error_category=CATEGORY_CANCELLED,
                            attempt=attempt + 1,
                        )
                    raise AgentRunCancelledError("Agent run was cancelled.") from exc
                if attempt + 1 < attempts and context is not None:
                    emit_event(
                        context,
                        "run.retry_scheduled",
                        error_category=classify_error(exc),
                        attempt=attempt + 1,
                        max_retries=max_retries,
                    )
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
        token = resolve_cancellation_token(cancel_event)
        try:
            started_at = perf_counter()
            for attempt in range(max_retries + 1):
                try:
                    token.raise_if_cancelled("execution")
                    with cancellation_scope(token):
                        for event in runtime.stream(definition, context):
                            if token.cancelled:
                                emit_event(
                                    context,
                                    "run.cancelled",
                                    error_category=CATEGORY_CANCELLED,
                                    attempt=attempt + 1,
                                )
                                raise AgentRunCancelledError(
                                    "Agent run was cancelled during execution."
                                )
                            if perf_counter() - started_at > timeout_seconds:
                                token.request_cancel("run timeout")
                                emit_event(
                                    context,
                                    "run.timeout",
                                    error_category=CATEGORY_RUN_TIMEOUT,
                                    attempt=attempt + 1,
                                    timeout_seconds=timeout_seconds,
                                )
                                raise AgentRunTimeoutError(
                                    f"Agent run exceeded {timeout_seconds:.1f}s "
                                    "timeout. In-flight model or HTTP calls cannot "
                                    "be interrupted and may still be draining."
                                )
                            if event.event_type in ("run.completed", "run.interrupted"):
                                result_metadata = {
                                    "runtime_type": compiled.spec.runtime_type
                                }
                                if event.data.get("interrupts"):
                                    result_metadata["interrupts"] = event.data[
                                        "interrupts"
                                    ]
                                completed_result = AgentRunResult(
                                    run_id=context.run_id,
                                    session_id=context.session_id,
                                    content=str(event.data.get("content", "")),
                                    status=(
                                        "interrupted"
                                        if event.event_type == "run.interrupted"
                                        else "completed"
                                    ),
                                    metadata=result_metadata,
                                )
                            yield event
                    break
                except (AgentRunCancelledError, AgentRunTimeoutError):
                    raise
                except Exception as exc:
                    if attempt >= max_retries:
                        raise
                    emit_event(
                        context,
                        "run.retry_scheduled",
                        error_category=classify_error(exc),
                        attempt=attempt + 1,
                        max_retries=max_retries,
                    )
        except KeyboardInterrupt:
            token.request_cancel("keyboard interrupt")
            emit_event(
                context,
                "run.cancelled",
                error_category=CATEGORY_CANCELLED,
                reason="keyboard_interrupt",
            )
            self._run_repository.save(
                record.transition("cancelled", error_type=CATEGORY_CANCELLED)
            )
            raise
        except Exception as exc:
            category = classify_error(exc)
            if isinstance(exc, AgentRunCancelledError):
                emit_event(context, "run.cancelled", error_category=category)
            elif isinstance(exc, AgentRunTimeoutError):
                emit_event(context, "run.timeout", error_category=category)
            self._run_repository.save(
                record.transition(
                    "cancelled" if isinstance(exc, AgentRunCancelledError) else "failed",
                    error_type=category,
                )
            )
            raise
        self._run_repository.save(
            record.transition(
                completed_result.status if completed_result else "failed",
                result=completed_result,
            )
        )

    def verify_resumable(self, thread_id: str) -> RunRecord | None:
        """Validate the resume contract and return the interrupted Run.

        A thread is resumable when it has persisted checkpoints and its latest
        Run is not already completed. Resuming a completed thread is rejected so
        that a repeated resume cannot replay the same side effects.
        """
        if not thread_id.strip():
            raise ValueError("thread_id must not be empty")
        if self._checkpoint_repository is None:
            raise RuntimeError("Checkpoint repository is not configured")
        if not self._checkpoint_repository.list(thread_id):
            raise ResumeNotAllowedError(
                f"Thread has no persisted checkpoint: {thread_id}"
            )
        runs = self._run_repository.list(session_id=thread_id)
        latest = runs[0] if runs else None
        if latest is not None and latest.status == "completed":
            raise ResumeNotAllowedError(
                f"Thread is already completed: {thread_id}"
            )
        return latest

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

    def delete_checkpoints(self, thread_id: str) -> None:
        if self._checkpoint_repository is None:
            raise RuntimeError("Checkpoint repository is not configured")
        if not thread_id.strip():
            raise ValueError("thread_id must not be empty")
        self._checkpoint_repository.delete_thread(thread_id)

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
            result = self._execute_with_policy(
                lambda: runtime.resume(definition, context),
                timeout_seconds=compiled.spec.timeout_seconds,
                cancel_event=context.metadata.get("cancel_event"),
                max_retries=int(context.metadata.get("max_retries", 0)),
                context=context,
            )
        except KeyboardInterrupt:
            emit_event(
                context,
                "run.cancelled",
                error_category=CATEGORY_CANCELLED,
                reason="keyboard_interrupt",
            )
            self._run_repository.save(
                record.transition("cancelled", error_type=CATEGORY_CANCELLED)
            )
            raise
        except Exception as exc:
            category = classify_error(exc)
            if isinstance(exc, AgentRunCancelledError):
                emit_event(context, "run.cancelled", error_category=category)
            elif isinstance(exc, AgentRunTimeoutError):
                emit_event(context, "run.timeout", error_category=category)
            self._run_repository.save(
                record.transition(
                    "cancelled" if isinstance(exc, AgentRunCancelledError) else "failed",
                    error_type=category,
                )
            )
            raise
        self._run_repository.save(record.transition(result.status, result=result))
        return result
