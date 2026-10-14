"""Service settings and safety gates loaded from environment variables."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from agent_platform.telemetry import TracingPolicy
from weather_agent.config import checkpoint_db_path_from_environment


class ServiceConfigurationError(RuntimeError):
    """Raised when service configuration is missing or unsafe."""


# --- explicit in-code key switch -------------------------------------------
# Fill this in to run the service without touching .env or the shell:
#
#     IN_CODE_API_KEYS = "team-a:0123456789abcdef0123"
#
# The format matches SERVICE_API_KEYS: 'tenant_id:api_key', comma-separated for
# several tenants. A non-empty SERVICE_API_KEYS still wins, so a deployment can
# override this value without editing code. Leave it empty to keep the
# fail-closed default of refusing to start without any tenant.
#
# WARNING: whatever is written here is committed to version control. Keep only
# throwaway local keys in it; real keys belong in .env (git-ignored) or in the
# deployment environment.
IN_CODE_API_KEYS: str = (
    "team-a:sk-8bb662f5bcdd6b2e4a2a40d14c1f63c6045f3d0531ac74a11e08550342692393"
)


@dataclass(frozen=True, slots=True)
class Tenant:
    """One authenticated caller namespace."""

    tenant_id: str
    api_key: str

    def __post_init__(self) -> None:
        if not self.tenant_id.strip():
            raise ServiceConfigurationError("tenant_id must not be empty")
        if ":" in self.tenant_id:
            raise ServiceConfigurationError(
                "tenant_id must not contain ':' because it is the thread namespace prefix"
            )
        if len(self.api_key) < 16:
            raise ServiceConfigurationError(
                "api_key must be at least 16 characters"
            )


def _is_loopback(host: str) -> bool:
    if host in {"", "localhost"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _parse_tenants(raw: str) -> tuple[Tenant, ...]:
    tenants: list[Tenant] = []
    for entry in raw.split(","):
        candidate = entry.strip()
        if not candidate:
            continue
        tenant_id, separator, api_key = candidate.partition(":")
        if not separator:
            raise ServiceConfigurationError(
                "SERVICE_API_KEYS entries must use the form 'tenant_id:api_key'"
            )
        tenants.append(Tenant(tenant_id=tenant_id.strip(), api_key=api_key.strip()))
    return tuple(tenants)


def _configured_api_keys() -> str:
    """Return the configured tenants string, preferring the environment.

    Precedence: a non-empty ``SERVICE_API_KEYS`` (process environment or a value
    loaded from ``.env``) wins, then :data:`IN_CODE_API_KEYS`. The environment
    wins so a deployment can override the checked-in value without editing code.
    An empty result still fails closed in
    :meth:`ServiceSettings.__post_init__`.
    """
    from_environment = os.getenv("SERVICE_API_KEYS", "").strip()
    if from_environment:
        return from_environment
    return IN_CODE_API_KEYS.strip()


def _int_env(name: str, default: int, *, minimum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ServiceConfigurationError(f"{name} must be an integer.") from exc
    if value < minimum:
        raise ServiceConfigurationError(f"{name} must be at least {minimum}.")
    return value


def _float_env(name: str, default: float, *, minimum: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ServiceConfigurationError(f"{name} must be a number.") from exc
    if value < minimum:
        raise ServiceConfigurationError(f"{name} must be at least {minimum}.")
    return value


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class ServiceSettings:
    """Everything the HTTP boundary needs to run safely."""

    tenants: tuple[Tenant, ...]
    host: str = "127.0.0.1"
    port: int = 8080
    rate_limit_per_minute: int = 60
    max_body_bytes: int = 20000
    request_timeout_seconds: float = 120.0
    audit_path: str = "observability/audit/service.jsonl"
    allow_public_exposure: bool = False
    checkpoint_db_path: str = "agent_runs.sqlite3"
    session_db_path: str | None = None
    web_directory: str = "web"
    tracing_policy: TracingPolicy = field(default_factory=TracingPolicy)

    @property
    def resolved_session_db_path(self) -> str:
        """Chat transcripts share the Run ledger file unless overridden."""
        return self.session_db_path or self.checkpoint_db_path

    def __post_init__(self) -> None:
        if not self.tenants:
            raise ServiceConfigurationError(
                "At least one tenant is required; set SERVICE_API_KEYS or fill "
                "IN_CODE_API_KEYS in service/config.py."
            )
        if not _is_loopback(self.host) and not self.allow_public_exposure:
            raise ServiceConfigurationError(
                f"Refusing to bind {self.host}: non-loopback binding requires "
                "SERVICE_ALLOW_PUBLIC_EXPOSURE=true. Keep the service local until "
                "authentication, rate limiting, tenant isolation and audit are "
                "reviewed for the target environment."
            )

    @classmethod
    def from_environment(cls) -> "ServiceSettings":
        """Load settings from the environment, failing closed on unsafe input."""
        load_dotenv()
        host = os.getenv("SERVICE_HOST", "127.0.0.1").strip() or "127.0.0.1"
        return cls(
            tenants=_parse_tenants(_configured_api_keys()),
            host=host,
            port=_int_env("SERVICE_PORT", 8080, minimum=1),
            rate_limit_per_minute=_int_env(
                "SERVICE_RATE_LIMIT_PER_MINUTE", 60, minimum=1
            ),
            max_body_bytes=_int_env("SERVICE_MAX_BODY_BYTES", 20000, minimum=256),
            request_timeout_seconds=_float_env(
                "SERVICE_REQUEST_TIMEOUT_SECONDS", 120.0, minimum=1.0
            ),
            audit_path=os.getenv(
                "SERVICE_AUDIT_PATH", "observability/audit/service.jsonl"
            ).strip()
            or "observability/audit/service.jsonl",
            allow_public_exposure=_bool_env("SERVICE_ALLOW_PUBLIC_EXPOSURE"),
            checkpoint_db_path=checkpoint_db_path_from_environment(),
            session_db_path=os.getenv("SERVICE_SESSION_DB_PATH", "").strip() or None,
            web_directory=os.getenv("SERVICE_WEB_DIR", "web").strip() or "web",
            tracing_policy=_tracing_policy(),
        )


def _tracing_policy() -> TracingPolicy:
    try:
        return TracingPolicy.from_environment()
    except ValueError as exc:
        raise ServiceConfigurationError(str(exc)) from exc
