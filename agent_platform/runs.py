"""Run lifecycle records and repositories."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import sqlite3
from threading import RLock
from typing import Protocol

from agent_platform.domain import AgentContext, AgentDefinition, AgentRunResult, RunStatus


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class RunRecord:
    """Platform record for one Agent execution."""

    run_id: str
    session_id: str
    agent_id: str
    agent_version: int
    runtime_type: str
    status: RunStatus
    created_at: str
    updated_at: str
    result: AgentRunResult | None = None
    error_type: str | None = None

    @classmethod
    def pending(
        cls,
        agent: AgentDefinition,
        context: AgentContext,
    ) -> "RunRecord":
        timestamp = _now()
        return cls(
            run_id=context.run_id,
            session_id=context.session_id,
            agent_id=agent.agent_id,
            agent_version=agent.version,
            runtime_type=agent.runtime_type,
            status="pending",
            created_at=timestamp,
            updated_at=timestamp,
        )

    def transition(
        self,
        status: RunStatus,
        *,
        result: AgentRunResult | None = None,
        error_type: str | None = None,
    ) -> "RunRecord":
        return replace(
            self,
            status=status,
            updated_at=_now(),
            result=result,
            error_type=error_type,
        )


@dataclass(frozen=True, slots=True)
class ThreadRecord:
    """Queryable summary for a conversation thread."""

    session_id: str
    run_count: int
    latest_run_id: str
    latest_status: RunStatus
    updated_at: str


class RunRepository(Protocol):
    """Persist and query Agent run lifecycle records."""

    def save(self, record: RunRecord) -> None:
        """Create or replace one run record."""

    def get(self, run_id: str) -> RunRecord:
        """Return one run record by ID."""

    def list(
        self,
        *,
        session_id: str | None = None,
        status: RunStatus | None = None,
        limit: int | None = None,
    ) -> tuple[RunRecord, ...]:
        """List runs, newest first."""

    def list_threads(self, *, limit: int | None = None) -> tuple[ThreadRecord, ...]:
        """List conversation threads derived from stored runs."""


class InMemoryRunRepository:
    """Thread-safe local Run repository used before database integration."""

    def __init__(self) -> None:
        self._records: dict[str, RunRecord] = {}
        self._lock = RLock()

    def save(self, record: RunRecord) -> None:
        with self._lock:
            self._records[record.run_id] = record

    def get(self, run_id: str) -> RunRecord:
        with self._lock:
            try:
                return self._records[run_id]
            except KeyError as exc:
                raise KeyError(f"Run not found: {run_id}") from exc

    def list(self, *, session_id=None, status=None, limit=None):
        with self._lock:
            records = list(self._records.values())
        if session_id is not None:
            records = [record for record in records if record.session_id == session_id]
        if status is not None:
            records = [record for record in records if record.status == status]
        records.sort(key=lambda record: record.updated_at, reverse=True)
        return tuple(records[:limit] if limit is not None else records)

    def list_threads(self, *, limit=None):
        records = self.list()
        grouped: dict[str, list[RunRecord]] = {}
        for record in records:
            grouped.setdefault(record.session_id, []).append(record)
        threads = [
            ThreadRecord(
                session_id=session_id,
                run_count=len(items),
                latest_run_id=items[0].run_id,
                latest_status=items[0].status,
                updated_at=items[0].updated_at,
            )
            for session_id, items in grouped.items()
        ]
        threads.sort(key=lambda item: item.updated_at, reverse=True)
        return tuple(threads[:limit] if limit is not None else threads)


class SQLiteRunRepository:
    """SQLite-backed Run repository for local durable execution records."""

    def __init__(self, path: str = "agent_runs.sqlite3") -> None:
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_runs (
                run_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                agent_version INTEGER NOT NULL,
                runtime_type TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                result_json TEXT,
                error_type TEXT
            )
            """
        )
        self._connection.commit()
        self._lock = RLock()

    def save(self, record: RunRecord) -> None:
        result_json = None
        if record.result is not None:
            result_json = json.dumps(
                {
                    "run_id": record.result.run_id,
                    "session_id": record.result.session_id,
                    "content": record.result.content,
                    "status": record.result.status,
                    "metadata": dict(record.result.metadata),
                },
                ensure_ascii=False,
            )
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO agent_runs (
                    run_id, session_id, agent_id, agent_version, runtime_type,
                    status, created_at, updated_at, result_json, error_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    session_id=excluded.session_id,
                    agent_id=excluded.agent_id,
                    agent_version=excluded.agent_version,
                    runtime_type=excluded.runtime_type,
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    result_json=excluded.result_json,
                    error_type=excluded.error_type
                """,
                (
                    record.run_id,
                    record.session_id,
                    record.agent_id,
                    record.agent_version,
                    record.runtime_type,
                    record.status,
                    record.created_at,
                    record.updated_at,
                    result_json,
                    record.error_type,
                ),
            )
            self._connection.commit()

    def get(self, run_id: str) -> RunRecord:
        with self._lock:
            row = self._connection.execute(
                "SELECT run_id, session_id, agent_id, agent_version, runtime_type, status, created_at, updated_at, result_json, error_type FROM agent_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Run not found: {run_id}")
        result = None
        if row[8]:
            payload = json.loads(row[8])
            result = AgentRunResult(
                run_id=payload["run_id"],
                session_id=payload["session_id"],
                content=payload["content"],
                status=payload["status"],
                metadata=payload.get("metadata", {}),
            )
        return RunRecord(
            run_id=row[0],
            session_id=row[1],
            agent_id=row[2],
            agent_version=row[3],
            runtime_type=row[4],
            status=row[5],
            created_at=row[6],
            updated_at=row[7],
            result=result,
            error_type=row[9],
        )

    def list(self, *, session_id=None, status=None, limit=None):
        query = "SELECT run_id FROM agent_runs"
        clauses: list[str] = []
        params: list[str] = []
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(str(limit))
        with self._lock:
            ids = [row[0] for row in self._connection.execute(query, params).fetchall()]
        return tuple(self.get(run_id) for run_id in ids)

    def list_threads(self, *, limit=None):
        query = """
            SELECT session_id, COUNT(*) AS run_count, run_id, status, updated_at
            FROM agent_runs AS latest
            WHERE updated_at = (
                SELECT MAX(candidate.updated_at)
                FROM agent_runs AS candidate
                WHERE candidate.session_id = latest.session_id
            )
            GROUP BY session_id
            ORDER BY updated_at DESC
        """
        if limit is not None:
            query += " LIMIT ?"
            params: tuple[object, ...] = (limit,)
        else:
            params = ()
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
            counts = dict(
                self._connection.execute(
                    "SELECT session_id, COUNT(*) FROM agent_runs GROUP BY session_id"
                ).fetchall()
            )
        return tuple(
            ThreadRecord(
                session_id=row[0],
                run_count=counts[row[0]],
                latest_run_id=row[2],
                latest_status=row[3],
                updated_at=row[4],
            )
            for row in rows
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SQLiteRunRepository":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
