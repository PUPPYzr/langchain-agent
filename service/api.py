"""Request models, stable error envelope and response payload builders."""

from __future__ import annotations

from typing import Any, Mapping

from pydantic import BaseModel, Field, ValidationError

from agent_platform.checkpoints import CheckpointRecord
from agent_platform.events import AgentEvent
from agent_platform.runs import RunRecord, ThreadRecord

API_VERSION = "v1"
API_PREFIX = f"/{API_VERSION}"

QUESTION_MAX_LENGTH = 2000

ERROR_STATUS: Mapping[str, int] = {
    "unauthorized": 401,
    "forbidden": 403,
    "invalid_request": 400,
    "not_found": 404,
    "method_not_allowed": 405,
    "not_acceptable": 406,
    "conflict": 409,
    "run_cancelled": 409,
    "payload_too_large": 413,
    "unsupported_media_type": 415,
    "rate_limited": 429,
    "run_failed": 502,
    "run_timeout": 504,
    "internal_error": 500,
}


class ServiceError(Exception):
    """A failure with a stable machine-readable code and HTTP status."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = ERROR_STATUS.get(code, 500)


class CreateRunRequest(BaseModel):
    """Body of ``POST /v1/runs``."""

    question: str = Field(min_length=1, max_length=QUESTION_MAX_LENGTH)
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    max_retries: int = Field(default=0, ge=0, le=2)


class CreateSessionRequest(BaseModel):
    """Body of ``POST /v1/sessions``."""

    title: str | None = Field(default=None, max_length=120)


class ChatRequest(BaseModel):
    """Body of ``POST /v1/chat``."""

    session_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    message: str = Field(min_length=1, max_length=QUESTION_MAX_LENGTH)
    max_retries: int = Field(default=0, ge=0, le=2)


class ResumeRunRequest(BaseModel):
    """Body of ``POST /v1/threads/{thread_id}/resume``.

    ``model_fields_set`` distinguishes "resume with None" (field omitted) from
    "resume with an explicit value" (including an explicit null).
    """

    resume_value: Any = None


def error_payload(
    code: str,
    message: str,
    request_id: str,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Build the stable error envelope."""
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id,
            "run_id": run_id,
        }
    }


def run_payload(record: RunRecord) -> dict[str, Any]:
    """Serialize a Run record without exposing framework objects."""
    result = record.result
    return {
        "run_id": record.run_id,
        "thread_id": record.session_id,
        "status": record.status,
        "agent_id": record.agent_id,
        "agent_version": record.agent_version,
        "runtime_type": record.runtime_type,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "error_category": record.error_type,
        "parent_run_id": record.parent_run_id,
        "content": result.content if result is not None else None,
        "metadata": dict(result.metadata) if result is not None else {},
    }


def thread_payload(record: ThreadRecord) -> dict[str, Any]:
    """Serialize a Thread summary."""
    return {
        "thread_id": record.session_id,
        "run_count": record.run_count,
        "latest_run_id": record.latest_run_id,
        "latest_status": record.latest_status,
        "updated_at": record.updated_at,
    }


def checkpoint_payload(record: CheckpointRecord) -> dict[str, Any]:
    """Serialize a Checkpoint summary."""
    return {
        "thread_id": record.thread_id,
        "checkpoint_id": record.checkpoint_id,
        "checkpoint_ns": record.checkpoint_ns,
        "parent_checkpoint_id": record.parent_checkpoint_id,
        "created_at": record.created_at,
        "metadata": dict(record.metadata),
    }


def event_payload(event: AgentEvent) -> dict[str, Any]:
    """Serialize one streamed Runtime event.

    Every Server-Sent Event frame uses the same envelope: a ``type`` field plus
    the correlation identifiers. Clients therefore have a single shape to parse.
    """
    return {
        "type": event.event_type,
        "run_id": event.run_id,
        "thread_id": event.session_id,
        "timestamp": event.timestamp,
        "data": dict(event.data),
    }


def session_payload(record: Any) -> dict[str, Any]:
    """Serialize a chat session for the Web UI."""
    return {
        "session_id": record.session_id,
        "title": record.title,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def message_payload(record: Any) -> dict[str, Any]:
    """Serialize one chat turn for the Web UI."""
    return {
        "sequence": record.sequence,
        "role": record.role,
        "content": record.content,
        "created_at": record.created_at,
    }


def validation_message(error: ValidationError) -> str:
    """Render a pydantic failure without leaking unrelated internals."""
    details = []
    for item in error.errors():
        location = ".".join(str(part) for part in item.get("loc", ()))
        details.append(f"{location or 'body'}: {item.get('msg', 'invalid')}")
    return "; ".join(details)
