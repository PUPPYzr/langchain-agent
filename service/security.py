"""Authentication, rate limiting and the audit trail for the HTTP boundary."""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any

from agent_platform.telemetry import TracingPolicy, redact
from service.config import Tenant


class Authenticator:
    """Resolve a presented API key to a tenant using constant-time comparison."""

    def __init__(self, tenants: tuple[Tenant, ...]) -> None:
        self._tenants = tuple(tenants)

    def authenticate(self, presented: str | None) -> Tenant | None:
        """Return the matching tenant, or ``None`` when authentication fails."""
        if not presented:
            return None
        candidate = presented.strip()
        matched: Tenant | None = None
        for tenant in self._tenants:
            # Compare every key so a partial match cannot be timed.
            if hmac.compare_digest(candidate, tenant.api_key):
                matched = tenant
        return matched


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float


class RateLimiter:
    """Per-tenant token bucket limiting requests per minute."""

    def __init__(self, *, limit_per_minute: int) -> None:
        if limit_per_minute < 1:
            raise ValueError("limit_per_minute must be >= 1")
        self._capacity = float(limit_per_minute)
        self._refill_per_second = limit_per_minute / 60.0
        self._buckets: dict[str, _Bucket] = {}
        self._lock = Lock()

    def check(self, tenant_id: str) -> float:
        """Consume one token; return 0.0 when allowed, else seconds to wait."""
        now = monotonic()
        with self._lock:
            bucket = self._buckets.get(tenant_id)
            if bucket is None:
                bucket = _Bucket(tokens=self._capacity, updated_at=now)
                self._buckets[tenant_id] = bucket
            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(
                self._capacity,
                bucket.tokens + elapsed * self._refill_per_second,
            )
            bucket.updated_at = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return 0.0
            missing = 1.0 - bucket.tokens
            return missing / self._refill_per_second


class AuditLog:
    """Append-only JSONL audit trail with the same redaction rules as traces."""

    def __init__(self, path: str | Path, policy: TracingPolicy | None = None) -> None:
        self.path = Path(path)
        self.policy = policy or TracingPolicy()

    def record(self, **fields: Any) -> None:
        """Append one audited request outcome."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **redact(fields, policy=self.policy),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
