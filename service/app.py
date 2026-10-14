"""HTTP boundary over ``AgentApplicationService``.

Every request is delegated to the same application service the CLI uses; this
module adds authentication, rate limiting, tenant isolation, idempotency,
request validation, a stable error envelope and an audit trail. It does not
re-implement Runtime, Compiler or Tool Registry logic.

Two ordering rules matter for correctness:

* the request body is always consumed before a response is written, otherwise
  the unread bytes are parsed as the next request on a kept-alive connection;
* the audit record is written *before* the response, so a request that produced
  a response can never be missing from the audit trail.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, RLock
from time import monotonic, perf_counter
from typing import Any, Callable, Iterator, cast
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from agent_platform import (
    AgentApplicationService,
    AgentContext,
    CheckpointRepository,
    ResumeNotAllowedError,
    RunRepository,
    SQLiteCheckpointRepository,
    SQLiteRunRepository,
)
from agent_platform.compiler import CompiledAgent
from agent_platform.domain import AgentRunCancelledError, AgentRunTimeoutError
from agent_platform.telemetry import classify_error
from service.api import (
    API_PREFIX,
    API_VERSION,
    ChatRequest,
    CreateRunRequest,
    CreateSessionRequest,
    ResumeRunRequest,
    ServiceError,
    checkpoint_payload,
    error_payload,
    event_payload,
    message_payload,
    run_payload,
    session_payload,
    thread_payload,
    validation_message,
)
from service.chat import ExecutionProjector
from service.config import ServiceSettings
from service.security import AuditLog, Authenticator, RateLimiter
from service.sessions import ChatSessionStore, SessionNotFoundError

IDEMPOTENCY_TTL_SECONDS = 3600.0
MAX_IDEMPOTENCY_KEY_LENGTH = 200
MAX_LIST_LIMIT = 200
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_RUNS_PATH = f"{API_PREFIX}/runs"
_THREADS_PATH = f"{API_PREFIX}/threads"
_HEALTH_PATH = f"{API_PREFIX}/health"
_SESSIONS_PATH = f"{API_PREFIX}/sessions"
_CHAT_PATH = f"{API_PREFIX}/chat"
_RUN_PATH_PATTERN = re.compile(rf"^{API_PREFIX}/runs/([^/]+)$")
_CHECKPOINTS_PATH_PATTERN = re.compile(rf"^{API_PREFIX}/threads/([^/]+)/checkpoints$")
_RESUME_PATH_PATTERN = re.compile(rf"^{API_PREFIX}/threads/([^/]+)/resume$")
_SESSION_MESSAGES_PATTERN = re.compile(rf"^{API_PREFIX}/sessions/([^/]+)/messages$")
_SESSION_PATH_PATTERN = re.compile(rf"^{API_PREFIX}/sessions/([^/]+)$")
_RUN_CANCEL_PATTERN = re.compile(rf"^{API_PREFIX}/runs/([^/]+)/cancel$")

STATIC_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".woff2": "font/woff2",
}

AgentProvider = Callable[[], CompiledAgent]


@dataclass(slots=True)
class _Cancellation:
    """A live cancel signal plus the thread that owns it."""

    event: Event
    thread_id: str


@dataclass(slots=True)
class _ResolvedResponse:
    """A response that has been decided but not yet written to the socket."""

    status: int
    payload: dict[str, Any] | None = None
    stream: Iterator[dict[str, Any]] | None = None
    run_id: str | None = None
    static_file: Path | None = None


class AgentService:
    """Tenant-scoped facade over the platform application service."""

    def __init__(
        self,
        *,
        run_repository: RunRepository,
        checkpoint_repository: CheckpointRepository,
        agent_provider: AgentProvider,
        settings: ServiceSettings,
        session_store: ChatSessionStore,
    ) -> None:
        self._settings = settings
        self._application = AgentApplicationService(
            run_repository=run_repository,
            checkpoint_repository=checkpoint_repository,
        )
        self._agent_provider = agent_provider
        self._sessions = session_store
        self._idempotency: dict[tuple[str, str], tuple[str, float]] = {}
        self._cancellations: dict[str, _Cancellation] = {}
        self._lock = RLock()

    # --- cancellation ------------------------------------------------------

    def _register_cancellation(self, run_id: str, thread_id: str) -> Event:
        """Create the cancel signal the Runtime observes for this run.

        The registration happens before the Run ledger row is written, so the
        browser can stop a run as soon as it receives the ``started`` event.
        """
        event = Event()
        with self._lock:
            self._cancellations[run_id] = _Cancellation(
                event=event,
                thread_id=thread_id,
            )
        return event

    def _release_cancellation(self, run_id: str) -> None:
        with self._lock:
            self._cancellations.pop(run_id, None)

    def cancel_run(self, *, tenant_id: str, run_id: str) -> bool:
        """Request cooperative cancellation of a run owned by the caller."""
        with self._lock:
            entry = self._cancellations.get(run_id)
        if entry is not None:
            self._require_owned(tenant_id, entry.thread_id)
            entry.event.set()
            return True
        # Not streaming any more: distinguish "unknown" from "already finished"
        # without leaking whether another tenant owns the run.
        try:
            record = self._application.get_run(run_id)
        except KeyError as exc:
            raise ServiceError("not_found", "run not found") from exc
        self._require_owned(tenant_id, record.session_id)
        raise ServiceError(
            "conflict",
            "run is not cancellable (already finished or not streaming)",
        )

    # --- tenant scoping ----------------------------------------------------

    @staticmethod
    def _prefix(tenant_id: str) -> str:
        return f"{tenant_id}:"

    def _thread_id(self, tenant_id: str, session_id: str | None) -> str:
        suffix = session_id or str(uuid4())
        if not SESSION_ID_PATTERN.match(suffix):
            raise ServiceError(
                "invalid_request",
                "session_id must match [A-Za-z0-9_-]{1,64}",
            )
        return f"{self._prefix(tenant_id)}{suffix}"

    def _require_owned(self, tenant_id: str, thread_id: str) -> None:
        if not thread_id.startswith(self._prefix(tenant_id)):
            # Deliberately indistinguishable from a genuinely missing thread.
            raise ServiceError("not_found", "thread not found")

    # --- idempotency -------------------------------------------------------

    def _idempotent_run_id(self, tenant_id: str, key: str | None) -> str | None:
        if not key:
            return None
        with self._lock:
            entry = self._idempotency.get((tenant_id, key))
            if entry is None:
                return None
            run_id, expires_at = entry
            if expires_at < monotonic():
                self._idempotency.pop((tenant_id, key), None)
                return None
            return run_id

    def _remember(self, tenant_id: str, key: str | None, run_id: str) -> None:
        if not key:
            return
        with self._lock:
            self._idempotency[(tenant_id, key)] = (
                run_id,
                monotonic() + IDEMPOTENCY_TTL_SECONDS,
            )

    # --- run lifecycle -----------------------------------------------------

    @staticmethod
    def _raise_for_run_failure(exc: Exception, run_id: str) -> None:
        if isinstance(exc, AgentRunCancelledError):
            raise ServiceError("run_cancelled", f"run {run_id} was cancelled") from exc
        if isinstance(exc, AgentRunTimeoutError):
            raise ServiceError("run_timeout", f"run {run_id} timed out") from exc
        raise ServiceError(
            "run_failed",
            f"run {run_id} failed ({classify_error(exc)})",
        ) from exc

    def _record(self, run_id: str) -> dict[str, Any]:
        try:
            return run_payload(self._application.get_run(run_id))
        except KeyError as exc:  # pragma: no cover - the service always persists
            raise ServiceError(
                "internal_error",
                "run record missing after execution",
            ) from exc

    def create_run(
        self,
        *,
        tenant_id: str,
        request: CreateRunRequest,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Execute one run synchronously and return its record."""
        existing = self._idempotent_run_id(tenant_id, idempotency_key)
        if existing is not None:
            return self._record(existing)
        context = self._build_context(tenant_id, request)
        try:
            self._application.run(self._agent_provider(), context)
        except ServiceError:
            raise
        except Exception as exc:
            self._raise_for_run_failure(exc, context.run_id)
        self._remember(tenant_id, idempotency_key, context.run_id)
        return self._record(context.run_id)

    def start_run_stream(
        self,
        *,
        tenant_id: str,
        request: CreateRunRequest,
        idempotency_key: str | None = None,
    ) -> tuple[str, Iterator[dict[str, Any]]]:
        """Return the Run id and a lazy iterator of normalized events."""
        existing = self._idempotent_run_id(tenant_id, idempotency_key)
        if existing is not None:
            return existing, iter((self._record(existing),))
        context = self._build_context(tenant_id, request)

        def events() -> Iterator[dict[str, Any]]:
            yield {"type": "run.accepted", "run_id": context.run_id}
            try:
                for event in self._application.stream(
                    self._agent_provider(),
                    context,
                ):
                    yield event_payload(event)
            except ServiceError:
                raise
            except AgentRunCancelledError as exc:
                raise ServiceError(
                    "run_cancelled",
                    f"run {context.run_id} was cancelled",
                ) from exc
            except AgentRunTimeoutError as exc:
                raise ServiceError(
                    "run_timeout",
                    f"run {context.run_id} timed out",
                ) from exc
            except Exception as exc:
                raise ServiceError(
                    "run_failed",
                    f"run {context.run_id} failed ({classify_error(exc)})",
                ) from exc
            self._remember(tenant_id, idempotency_key, context.run_id)

        return context.run_id, events()

    def resume(
        self,
        *,
        tenant_id: str,
        thread_id: str,
        resume_value: Any,
        has_resume_value: bool,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Resume an interrupted thread, creating a new Run on the same thread."""
        self._require_owned(tenant_id, thread_id)
        existing = self._idempotent_run_id(tenant_id, idempotency_key)
        if existing is not None:
            return self._record(existing)
        try:
            parent_run = self._application.verify_resumable(thread_id)
        except ResumeNotAllowedError as exc:
            raise ServiceError("conflict", str(exc)) from exc
        except ValueError as exc:
            raise ServiceError("invalid_request", str(exc)) from exc
        run_id = str(uuid4())
        metadata: dict[str, Any] = {}
        if has_resume_value:
            metadata["resume_value"] = resume_value
        if parent_run is not None:
            metadata["parent_run_id"] = parent_run.run_id
        context = AgentContext(
            question="Resume interrupted run",
            run_id=run_id,
            session_id=thread_id,
            metadata=metadata,
        )
        try:
            self._application.resume(self._agent_provider(), context)
        except ServiceError:
            raise
        except Exception as exc:
            self._raise_for_run_failure(exc, run_id)
        self._remember(tenant_id, idempotency_key, run_id)
        return self._record(run_id)

    # --- queries -----------------------------------------------------------

    def get_run(self, *, tenant_id: str, run_id: str) -> dict[str, Any]:
        """Return one Run, hiding other tenants' runs as not-found."""
        try:
            record = self._application.get_run(run_id)
        except KeyError as exc:
            raise ServiceError("not_found", "run not found") from exc
        self._require_owned(tenant_id, record.session_id)
        return run_payload(record)

    def list_runs(
        self,
        *,
        tenant_id: str,
        session_id: str | None,
        status: str | None,
        limit: int | None,
    ) -> list[dict[str, Any]]:
        """List Runs owned by the caller."""
        prefix = self._prefix(tenant_id)
        if session_id is not None:
            if not SESSION_ID_PATTERN.match(session_id):
                raise ServiceError(
                    "invalid_request",
                    "session_id must match [A-Za-z0-9_-]{1,64}",
                )
            records = self._application.list_runs(
                session_id=f"{prefix}{session_id}",
                status=status,
                limit=limit,
            )
        else:
            owned = tuple(
                record
                for record in self._application.list_runs(status=status)
                if record.session_id.startswith(prefix)
            )
            records = owned[:limit] if limit is not None else owned
        return [run_payload(record) for record in records]

    def list_threads(
        self,
        *,
        tenant_id: str,
        limit: int | None,
    ) -> list[dict[str, Any]]:
        """List Threads owned by the caller."""
        prefix = self._prefix(tenant_id)
        owned = [
            thread
            for thread in self._application.list_threads()
            if thread.session_id.startswith(prefix)
        ]
        if limit is not None:
            owned = owned[:limit]
        return [thread_payload(thread) for thread in owned]

    def list_checkpoints(
        self,
        *,
        tenant_id: str,
        thread_id: str,
        limit: int | None,
    ) -> list[dict[str, Any]]:
        """List Checkpoints for an owned thread."""
        self._require_owned(tenant_id, thread_id)
        return [
            checkpoint_payload(record)
            for record in self._application.list_checkpoints(
                thread_id,
                limit=limit,
            )
        ]

    def delete_checkpoints(self, *, tenant_id: str, thread_id: str) -> None:
        """Delete all Checkpoints for an owned thread."""
        self._require_owned(tenant_id, thread_id)
        try:
            self._application.delete_checkpoints(thread_id)
        except ValueError as exc:
            raise ServiceError("invalid_request", str(exc)) from exc

    # --- chat sessions -----------------------------------------------------

    def context_mode(self) -> str:
        """Report whether the Agent preserves multi-turn context.

        ``checkpointed`` means the LangGraph checkpointer carries the thread
        history; ``stateless`` means each turn is independent and the Web UI
        must be read as a transcript, not as Agent memory.
        """
        try:
            runtime_type = self._agent_provider().spec.runtime_type
        except Exception:
            return "unknown"
        return "checkpointed" if runtime_type == "langgraph" else "stateless"

    def list_sessions(
        self,
        *,
        tenant_id: str,
        limit: int | None,
    ) -> list[dict[str, Any]]:
        """List the caller's chat sessions."""
        return [
            session_payload(session)
            for session in self._sessions.list(tenant_id, limit=limit)
        ]

    def create_session(
        self,
        *,
        tenant_id: str,
        title: str | None,
    ) -> dict[str, Any]:
        """Create a new chat session."""
        return session_payload(self._sessions.create(tenant_id, title or "新对话"))

    def delete_session(self, *, tenant_id: str, session_id: str) -> None:
        """Delete one of the caller's chat sessions."""
        try:
            self._sessions.delete(tenant_id, session_id)
        except SessionNotFoundError as exc:
            raise ServiceError("not_found", "session not found") from exc

    def session_messages(
        self,
        *,
        tenant_id: str,
        session_id: str,
    ) -> list[dict[str, Any]]:
        """Return the transcript of one of the caller's sessions."""
        try:
            records = self._sessions.messages(tenant_id, session_id)
        except SessionNotFoundError as exc:
            raise ServiceError("not_found", "session not found") from exc
        return [message_payload(record) for record in records]

    def chat_stream(
        self,
        *,
        tenant_id: str,
        session_id: str,
        message: str,
        max_retries: int = 0,
    ) -> tuple[str, Iterator[dict[str, Any]]]:
        """Run one chat turn on an existing session and stream UI events."""
        try:
            self._sessions.get(tenant_id, session_id)
        except SessionNotFoundError as exc:
            raise ServiceError("not_found", "session not found") from exc
        thread_id = self._thread_id(tenant_id, session_id)
        run_id = str(uuid4())
        cancel_event = self._register_cancellation(run_id, thread_id)
        self._sessions.append_message(tenant_id, session_id, "user", message)
        context = AgentContext(
            question=message,
            run_id=run_id,
            session_id=thread_id,
            metadata={"max_retries": max_retries, "cancel_event": cancel_event},
        )

        def events() -> Iterator[dict[str, Any]]:
            projector = ExecutionProjector()
            answer = ""
            yield {"type": "started", "session_id": session_id, "run_id": run_id}
            try:
                for event in self._application.stream(
                    self._agent_provider(),
                    context,
                ):
                    for item in projector.project(event):
                        if item["type"] == "answer":
                            answer = item["content"]
                        yield item
            except AgentRunCancelledError:
                yield {
                    "type": "error",
                    "code": "run_cancelled",
                    "message": "运行已取消。",
                }
                return
            except AgentRunTimeoutError:
                yield {
                    "type": "error",
                    "code": "run_timeout",
                    "message": "运行超时。",
                }
                return
            except Exception as exc:
                yield {
                    "type": "error",
                    "code": "run_failed",
                    "message": f"Agent 执行失败（{classify_error(exc)}）。",
                }
                return
            saved = False
            if answer.strip():
                self._sessions.append_message(
                    tenant_id,
                    session_id,
                    "assistant",
                    answer,
                )
                saved = True
            yield {
                "type": "done",
                "session_id": session_id,
                "run_id": run_id,
                "saved": saved,
            }

        def guarded_events() -> Iterator[dict[str, Any]]:
            try:
                yield from events()
            finally:
                self._release_cancellation(run_id)

        return run_id, guarded_events()

    # --- helpers -----------------------------------------------------------

    def _build_context(
        self,
        tenant_id: str,
        request: CreateRunRequest,
    ) -> AgentContext:
        return AgentContext(
            question=request.question,
            run_id=str(uuid4()),
            session_id=self._thread_id(tenant_id, request.session_id),
            metadata={"max_retries": request.max_retries},
        )


class AgentHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server carrying the resolved service dependencies."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        service: AgentService,
        settings: ServiceSettings,
        authenticator: Authenticator,
        rate_limiter: RateLimiter,
        audit: AuditLog,
        run_repository: RunRepository,
        checkpoint_repository: CheckpointRepository,
        session_store: ChatSessionStore,
        owns_repositories: bool,
        owns_session_store: bool,
        web_directory: Path | None = None,
    ) -> None:
        self.service = service
        self.settings = settings
        self.authenticator = authenticator
        self.rate_limiter = rate_limiter
        self.audit = audit
        self.run_repository = run_repository
        self.checkpoint_repository = checkpoint_repository
        self.session_store = session_store
        self.owns_repositories = owns_repositories
        self.owns_session_store = owns_session_store
        self.web_directory = web_directory
        super().__init__(address, ServiceRequestHandler)

    def server_close(self) -> None:
        super().server_close()
        if self.owns_repositories:
            self.run_repository.close()
            self.checkpoint_repository.close()
        if self.owns_session_store:
            self.session_store.close()


class ServiceRequestHandler(BaseHTTPRequestHandler):
    """Route one HTTP request to :class:`AgentService`."""

    protocol_version = "HTTP/1.1"
    server_version = "weather-agent-service"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence the default access log; outcomes go to the audit trail."""

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    # --- request lifecycle -------------------------------------------------

    def _handle(self, method: str) -> None:
        self._request_id = str(uuid4())
        self._raw_body = b""
        started = perf_counter()
        tenant_id: str | None = None
        resolved: _ResolvedResponse | None = None
        outcome = "internal_error"
        status_code = 500
        retry_after: int | None = None
        try:
            server = self._server()
            self.connection.settimeout(server.settings.request_timeout_seconds)
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if method == "GET" and path in {"/health", _HEALTH_PATH}:
                resolved = _ResolvedResponse(
                    status=200,
                    payload={
                        "status": "ok",
                        "api_version": API_VERSION,
                        "context_mode": self._server().service.context_mode(),
                    },
                )
            elif method == "GET" and not path.startswith(API_PREFIX):
                # The Web UI is a static asset; it is not an authenticated API.
                resolved = self._resolve_static(path)
            else:
                # Consume the body before any early return, otherwise the unread
                # bytes are parsed as the next request on a kept-alive socket.
                self._consume_body(server)
                tenant = server.authenticator.authenticate(self._presented_key())
                if tenant is None:
                    raise ServiceError("unauthorized", "missing or invalid API key")
                tenant_id = tenant.tenant_id
                wait = server.rate_limiter.check(tenant_id)
                if wait > 0:
                    retry_after = max(1, int(wait + 0.5))
                    raise ServiceError("rate_limited", "rate limit exceeded")
                resolved = self._resolve_route(method, path, query, tenant_id)
            status_code, outcome = resolved.status, "ok"
        except ServiceError as error:
            status_code, outcome = error.status, error.code
            resolved = _ResolvedResponse(
                status=error.status,
                payload=error_payload(
                    error.code,
                    error.message,
                    self._request_id,
                ),
            )
        except Exception:
            resolved = _ResolvedResponse(
                status=500,
                payload=error_payload(
                    "internal_error",
                    "unexpected server error",
                    self._request_id,
                ),
            )
        # Audit before emitting so a request that produced a response can never
        # be missing from the trail.
        self._server().audit.record(
            request_id=self._request_id,
            tenant=tenant_id,
            method=method,
            path=self.path,
            status=status_code,
            outcome=outcome,
            run_id=resolved.run_id if resolved is not None else None,
            duration_ms=round((perf_counter() - started) * 1000, 3),
            client=self.client_address[0] if self.client_address else None,
            idempotency_key=self._idempotency_key(),
        )
        self._emit(resolved, retry_after=retry_after)

    def _emit(
        self,
        resolved: _ResolvedResponse | None,
        *,
        retry_after: int | None,
    ) -> None:
        if resolved is None:
            return
        if resolved.static_file is not None:
            self._send_file(resolved.static_file)
            return
        if resolved.stream is not None:
            self._begin_stream()
            self._write_event({"type": "stream.opened", "run_id": resolved.run_id})
            try:
                for payload in resolved.stream:
                    self._write_event(payload)
            except ServiceError as error:
                # Same envelope as every other frame so clients parse one shape.
                self._write_event(
                    {
                        "type": "error",
                        "code": error.code,
                        "message": error.message,
                        "run_id": resolved.run_id,
                    }
                )
            finally:
                self._write_event(
                    {"type": "stream.closed", "run_id": resolved.run_id}
                )
            return
        self._send_json(resolved.status, resolved.payload, retry_after=retry_after)

    def _resolve_route(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        tenant_id: str,
    ) -> _ResolvedResponse:
        service = self._server().service
        if path == _RUNS_PATH:
            if method == "POST":
                request = self._read_model(CreateRunRequest)
                if self._wants_stream():
                    run_id, events = service.start_run_stream(
                        tenant_id=tenant_id,
                        request=request,
                        idempotency_key=self._idempotency_key(),
                    )
                    return _ResolvedResponse(
                        status=200,
                        stream=events,
                        run_id=run_id,
                    )
                payload = service.create_run(
                    tenant_id=tenant_id,
                    request=request,
                    idempotency_key=self._idempotency_key(),
                )
                return _ResolvedResponse(
                    status=201,
                    payload=payload,
                    run_id=payload["run_id"],
                )
            if method == "GET":
                return _ResolvedResponse(
                    status=200,
                    payload={
                        "runs": service.list_runs(
                            tenant_id=tenant_id,
                            session_id=self._single(query, "session_id"),
                            status=self._single(query, "status"),
                            limit=self._limit(query),
                        )
                    },
                )
            raise self._method_not_allowed(method, path)

        match = _RUN_CANCEL_PATTERN.match(path)
        if match:
            if method != "POST":
                raise self._method_not_allowed(method, path)
            run_id = match.group(1)
            service.cancel_run(tenant_id=tenant_id, run_id=run_id)
            return _ResolvedResponse(
                status=200,
                payload={"cancel_requested": True, "run_id": run_id},
                run_id=run_id,
            )

        match = _RUN_PATH_PATTERN.match(path)
        if match:
            if method != "GET":
                raise self._method_not_allowed(method, path)
            run_id = match.group(1)
            return _ResolvedResponse(
                status=200,
                payload=service.get_run(tenant_id=tenant_id, run_id=run_id),
                run_id=run_id,
            )

        if path == _THREADS_PATH:
            if method != "GET":
                raise self._method_not_allowed(method, path)
            return _ResolvedResponse(
                status=200,
                payload={
                    "threads": service.list_threads(
                        tenant_id=tenant_id,
                        limit=self._limit(query),
                    )
                },
            )

        match = _CHECKPOINTS_PATH_PATTERN.match(path)
        if match:
            thread_id = match.group(1)
            if method == "GET":
                return _ResolvedResponse(
                    status=200,
                    payload={
                        "checkpoints": service.list_checkpoints(
                            tenant_id=tenant_id,
                            thread_id=thread_id,
                            limit=self._limit(query),
                        )
                    },
                )
            if method == "DELETE":
                service.delete_checkpoints(tenant_id=tenant_id, thread_id=thread_id)
                return _ResolvedResponse(
                    status=200,
                    payload={"deleted": True, "thread_id": thread_id},
                )
            raise self._method_not_allowed(method, path)

        match = _RESUME_PATH_PATTERN.match(path)
        if match:
            if method != "POST":
                raise self._method_not_allowed(method, path)
            request = self._read_model(ResumeRunRequest)
            payload = service.resume(
                tenant_id=tenant_id,
                thread_id=match.group(1),
                resume_value=request.resume_value,
                has_resume_value="resume_value" in request.model_fields_set,
                idempotency_key=self._idempotency_key(),
            )
            return _ResolvedResponse(
                status=200,
                payload=payload,
                run_id=payload["run_id"],
            )

        if path == _SESSIONS_PATH:
            if method == "GET":
                return _ResolvedResponse(
                    status=200,
                    payload={
                        "sessions": service.list_sessions(
                            tenant_id=tenant_id,
                            limit=self._limit(query),
                        )
                    },
                )
            if method == "POST":
                request = self._read_model(CreateSessionRequest)
                return _ResolvedResponse(
                    status=201,
                    payload=service.create_session(
                        tenant_id=tenant_id,
                        title=request.title,
                    ),
                )
            raise self._method_not_allowed(method, path)

        if path == _CHAT_PATH:
            if method != "POST":
                raise self._method_not_allowed(method, path)
            request = self._read_model(ChatRequest)
            run_id, events = service.chat_stream(
                tenant_id=tenant_id,
                session_id=request.session_id,
                message=request.message,
                max_retries=request.max_retries,
            )
            return _ResolvedResponse(status=200, stream=events, run_id=run_id)

        match = _SESSION_MESSAGES_PATTERN.match(path)
        if match:
            if method != "GET":
                raise self._method_not_allowed(method, path)
            return _ResolvedResponse(
                status=200,
                payload={
                    "messages": service.session_messages(
                        tenant_id=tenant_id,
                        session_id=match.group(1),
                    )
                },
            )

        match = _SESSION_PATH_PATTERN.match(path)
        if match:
            if method != "DELETE":
                raise self._method_not_allowed(method, path)
            session_id = match.group(1)
            service.delete_session(tenant_id=tenant_id, session_id=session_id)
            return _ResolvedResponse(
                status=200,
                payload={"deleted": True, "session_id": session_id},
            )

        raise ServiceError("not_found", f"unknown path: {path}")

    # --- static Web UI -----------------------------------------------------

    def _resolve_static(self, path: str) -> _ResolvedResponse:
        web_root = self._server().web_directory
        if web_root is None or not web_root.exists():
            raise ServiceError(
                "not_found",
                "Web interface is not installed (SERVICE_WEB_DIR missing)",
            )
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        root = web_root.resolve()
        candidate = (root / relative).resolve()
        if not candidate.is_file() or root not in candidate.parents:
            raise ServiceError("not_found", "resource not found")
        return _ResolvedResponse(status=200, static_file=candidate)

    # --- request parsing ---------------------------------------------------

    def _consume_body(self, server: AgentHTTPServer) -> None:
        """Read the whole request body so the connection stays in sync."""
        self._raw_body = b""
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            return
        try:
            length = int(length_header)
        except ValueError as exc:
            self.close_connection = True
            raise ServiceError(
                "invalid_request",
                "Content-Length must be an integer",
            ) from exc
        if length < 0:
            self.close_connection = True
            raise ServiceError(
                "invalid_request",
                "Content-Length must not be negative",
            )
        if length > server.settings.max_body_bytes:
            # An oversized body cannot be drained safely, so drop the connection.
            self.close_connection = True
            raise ServiceError(
                "payload_too_large",
                f"body exceeds {server.settings.max_body_bytes} bytes",
            )
        if length:
            self._raw_body = self.rfile.read(length)

    def _read_model(self, model: type[BaseModel]) -> Any:
        if not self._raw_body:
            raise ServiceError("invalid_request", "request body is required")
        content_type = (
            (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        )
        if content_type != "application/json":
            raise ServiceError(
                "unsupported_media_type",
                "Content-Type must be application/json",
            )
        try:
            payload = json.loads(self._raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ServiceError(
                "invalid_request",
                "body must be a UTF-8 JSON object",
            ) from exc
        if not isinstance(payload, dict):
            raise ServiceError("invalid_request", "body must be a JSON object")
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            raise ServiceError("invalid_request", validation_message(exc)) from exc

    # --- response helpers --------------------------------------------------

    @staticmethod
    def _method_not_allowed(method: str, path: str) -> ServiceError:
        return ServiceError("method_not_allowed", f"{method} not allowed on {path}")

    def _presented_key(self) -> str | None:
        header = self.headers.get("Authorization") or ""
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return self.headers.get("X-API-Key")

    @staticmethod
    def _single(query: dict[str, list[str]], name: str) -> str | None:
        values = query.get(name)
        return values[0] if values else None

    def _limit(self, query: dict[str, list[str]]) -> int | None:
        raw = self._single(query, "limit")
        if raw is None:
            return None
        try:
            limit = int(raw)
        except ValueError as exc:
            raise ServiceError("invalid_request", "limit must be an integer") from exc
        if not 1 <= limit <= MAX_LIST_LIMIT:
            raise ServiceError(
                "invalid_request",
                f"limit must be between 1 and {MAX_LIST_LIMIT}",
            )
        return limit

    def _idempotency_key(self) -> str | None:
        raw = self.headers.get("Idempotency-Key")
        if raw is None:
            return None
        key = raw.strip()
        if not key:
            return None
        if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
            raise ServiceError(
                "invalid_request",
                "Idempotency-Key must be at most "
                f"{MAX_IDEMPOTENCY_KEY_LENGTH} characters",
            )
        return key

    def _wants_stream(self) -> bool:
        accept = self.headers.get("Accept") or ""
        return "text/event-stream" in accept.lower()

    def _server(self) -> AgentHTTPServer:
        return cast(AgentHTTPServer, self.server)

    def _send_json(
        self,
        status: int,
        payload: Any,
        *,
        retry_after: int | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-Id", self._request_id)
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        body = path.read_bytes()
        content_type = STATIC_CONTENT_TYPES.get(
            path.suffix.lower(),
            "application/octet-stream",
        )
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Request-Id", self._request_id)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _begin_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Request-Id", self._request_id)
        self.send_header("Connection", "close")
        self.end_headers()
        # No Content-Length: the client reads events until the socket closes.
        self.close_connection = True

    def _write_event(self, payload: dict[str, Any]) -> None:
        frame = (
            "data: " + json.dumps(payload, ensure_ascii=False, default=str) + "\n\n"
        ).encode("utf-8")
        self.wfile.write(frame)
        self.wfile.flush()


def default_agent_provider() -> AgentProvider:
    """Build the weather Agent once and reuse it across requests."""
    cached: dict[str, CompiledAgent] = {}
    lock = Lock()

    def provider() -> CompiledAgent:
        with lock:
            if "agent" not in cached:
                from weather_agent.agent import build_compiled_weather_agent
                from weather_agent.config import Settings

                cached["agent"] = build_compiled_weather_agent(
                    Settings.from_environment()
                )
            return cached["agent"]

    return provider


def create_server(
    settings: ServiceSettings,
    *,
    agent_provider: AgentProvider | None = None,
    run_repository: RunRepository | None = None,
    checkpoint_repository: CheckpointRepository | None = None,
    session_store: ChatSessionStore | None = None,
    web_directory: Path | None = None,
) -> AgentHTTPServer:
    """Build a ready-to-serve HTTP server for the given settings."""
    owns_repositories = run_repository is None and checkpoint_repository is None
    resolved_run_repository = run_repository or SQLiteRunRepository(
        settings.checkpoint_db_path
    )
    resolved_checkpoint_repository = (
        checkpoint_repository
        or SQLiteCheckpointRepository(settings.checkpoint_db_path)
    )
    owns_session_store = session_store is None
    resolved_session_store = session_store or ChatSessionStore(
        settings.resolved_session_db_path
    )
    resolved_web_directory = (
        web_directory
        if web_directory is not None
        else Path(settings.web_directory)
    )
    service = AgentService(
        run_repository=resolved_run_repository,
        checkpoint_repository=resolved_checkpoint_repository,
        agent_provider=agent_provider or default_agent_provider(),
        settings=settings,
        session_store=resolved_session_store,
    )
    return AgentHTTPServer(
        (settings.host, settings.port),
        service=service,
        settings=settings,
        authenticator=Authenticator(settings.tenants),
        rate_limiter=RateLimiter(limit_per_minute=settings.rate_limit_per_minute),
        audit=AuditLog(settings.audit_path, settings.tracing_policy),
        run_repository=resolved_run_repository,
        checkpoint_repository=resolved_checkpoint_repository,
        session_store=resolved_session_store,
        owns_repositories=owns_repositories,
        owns_session_store=owns_session_store,
        web_directory=resolved_web_directory,
    )
