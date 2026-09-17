"""Platform checkpoint provider contracts and durable LangGraph storage."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
import pickle
import sqlite3
from threading import Lock
from typing import Any, Mapping, Protocol

from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.memory import InMemorySaver


class CheckpointProvider(Protocol):
    """Create a runtime-specific checkpointer behind a platform boundary."""

    def create_checkpointer(self) -> Any:
        """Return a checkpointer accepted by the selected runtime."""


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    """Queryable summary of one persisted LangGraph checkpoint."""

    thread_id: str
    checkpoint_id: str
    checkpoint_ns: str
    created_at: str
    parent_checkpoint_id: str | None
    metadata: Mapping[str, Any]


class CheckpointRepository(Protocol):
    """Query checkpoint summaries without exposing LangGraph internals."""

    def get(self, thread_id: str, checkpoint_id: str | None = None) -> CheckpointRecord:
        """Return the requested or latest checkpoint for a thread."""

    def list(
        self,
        thread_id: str | None = None,
        *,
        limit: int | None = None,
    ) -> tuple[CheckpointRecord, ...]:
        """List checkpoint summaries, newest first."""


class NullCheckpointProvider:
    """Disable checkpoint persistence explicitly."""

    def create_checkpointer(self) -> None:
        return None


class InMemoryLangGraphCheckpointProvider:
    """Create LangGraph's in-memory checkpointer for local runs and tests."""

    def create_checkpointer(self) -> Any:
        return InMemorySaver()


class SQLiteLangGraphCheckpointSaver(InMemorySaver):
    """LangGraph saver backed by SQLite for process-restart recovery.

    The in-memory parent class still handles LangGraph's write protocol. A
    serialized full checkpoint snapshot is mirrored to SQLite after every put,
    allowing a fresh process to read the latest state without requiring the
    optional ``langgraph-checkpoint-sqlite`` package.
    """

    def __init__(self, path: str) -> None:
        super().__init__()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_checkpoints (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                parent_checkpoint_id TEXT,
                checkpoint_blob BLOB NOT NULL,
                metadata_blob BLOB NOT NULL,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
            )
            """
        )
        self._connection.commit()
        self._lock = Lock()

    def put(self, config, checkpoint, metadata, new_versions):
        updated = super().put(config, checkpoint, metadata, new_versions)
        configurable = config["configurable"]
        thread_id = configurable["thread_id"]
        checkpoint_ns = configurable.get("checkpoint_ns", "")
        checkpoint_id = checkpoint["id"]
        parent_id = configurable.get("checkpoint_id")
        snapshot = pickle.dumps(checkpoint, protocol=pickle.HIGHEST_PROTOCOL)
        metadata_blob = pickle.dumps(metadata, protocol=pickle.HIGHEST_PROTOCOL)
        with self._lock:
            self._connection.execute(
                """
                INSERT OR REPLACE INTO agent_checkpoints (
                    thread_id, checkpoint_ns, checkpoint_id, created_at,
                    parent_checkpoint_id, checkpoint_blob, metadata_blob
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    thread_id,
                    checkpoint_ns,
                    checkpoint_id,
                    datetime.now(timezone.utc).isoformat(),
                    parent_id,
                    snapshot,
                    metadata_blob,
                ),
            )
            self._connection.commit()
        return updated

    def get_tuple(self, config):
        configurable = config["configurable"]
        thread_id = configurable["thread_id"]
        checkpoint_ns = configurable.get("checkpoint_ns", "")
        checkpoint_id = configurable.get("checkpoint_id")
        with self._lock:
            if checkpoint_id:
                row = self._connection.execute(
                    "SELECT checkpoint_id, parent_checkpoint_id, checkpoint_blob, metadata_blob FROM agent_checkpoints WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?",
                    (thread_id, checkpoint_ns, checkpoint_id),
                ).fetchone()
            else:
                row = self._connection.execute(
                    "SELECT checkpoint_id, parent_checkpoint_id, checkpoint_blob, metadata_blob FROM agent_checkpoints WHERE thread_id = ? AND checkpoint_ns = ? ORDER BY checkpoint_id DESC LIMIT 1",
                    (thread_id, checkpoint_ns),
                ).fetchone()
        if row is None:
            return super().get_tuple(config)
        selected_id, parent_id, checkpoint_blob, metadata_blob = row
        selected_config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": selected_id,
            }
        }
        parent_config = None
        if parent_id:
            parent_config = {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": parent_id,
                }
            }
        return CheckpointTuple(
            config=selected_config,
            checkpoint=pickle.loads(checkpoint_blob),
            metadata=pickle.loads(metadata_blob),
            pending_writes=[],
            parent_config=parent_config,
        )

    def list(self, config, *, filter=None, before=None, limit=None) -> Iterator[CheckpointTuple]:
        configurable = (config or {}).get("configurable", {})
        thread_id = configurable.get("thread_id")
        checkpoint_ns = configurable.get("checkpoint_ns", "")
        query = "SELECT thread_id, checkpoint_id, parent_checkpoint_id, checkpoint_blob, metadata_blob FROM agent_checkpoints WHERE checkpoint_ns = ?"
        params: list[Any] = [checkpoint_ns]
        if thread_id:
            query += " AND thread_id = ?"
            params.append(thread_id)
        query += " ORDER BY checkpoint_id DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        for current_thread, checkpoint_id, parent_id, checkpoint_blob, metadata_blob in rows:
            yield CheckpointTuple(
                config={"configurable": {"thread_id": current_thread, "checkpoint_ns": checkpoint_ns, "checkpoint_id": checkpoint_id}},
                checkpoint=pickle.loads(checkpoint_blob),
                metadata=pickle.loads(metadata_blob),
                pending_writes=[],
                parent_config=None if not parent_id else {"configurable": {"thread_id": current_thread, "checkpoint_ns": checkpoint_ns, "checkpoint_id": parent_id}},
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class SQLiteCheckpointProvider:
    """Create a durable LangGraph checkpointer and query repository."""

    def __init__(self, path: str = "agent_runs.sqlite3") -> None:
        self.path = path

    def create_checkpointer(self) -> SQLiteLangGraphCheckpointSaver:
        return SQLiteLangGraphCheckpointSaver(self.path)

    def repository(self) -> "SQLiteCheckpointRepository":
        return SQLiteCheckpointRepository(self.path)


class SQLiteCheckpointRepository:
    """Read checkpoint summaries from the SQLite saver database."""

    def __init__(self, path: str = "agent_runs.sqlite3") -> None:
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_checkpoints (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                parent_checkpoint_id TEXT,
                checkpoint_blob BLOB NOT NULL,
                metadata_blob BLOB NOT NULL,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
            )
            """
        )
        self._connection.commit()
        self._lock = Lock()

    def list(self, thread_id: str | None = None, *, limit: int | None = None) -> tuple[CheckpointRecord, ...]:
        query = "SELECT thread_id, checkpoint_id, checkpoint_ns, created_at, parent_checkpoint_id, metadata_blob FROM agent_checkpoints"
        params: list[Any] = []
        if thread_id is not None:
            query += " WHERE thread_id = ?"
            params.append(thread_id)
        query += " ORDER BY created_at DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return tuple(
            CheckpointRecord(
                thread_id=row[0],
                checkpoint_id=row[1],
                checkpoint_ns=row[2],
                created_at=row[3],
                parent_checkpoint_id=row[4],
                metadata=pickle.loads(row[5]),
            )
            for row in rows
        )

    def get(self, thread_id: str, checkpoint_id: str | None = None) -> CheckpointRecord:
        records = self.list(thread_id)
        if checkpoint_id is not None:
            records = tuple(item for item in records if item.checkpoint_id == checkpoint_id)
        if not records:
            raise KeyError(f"Checkpoint not found: {thread_id}/{checkpoint_id or 'latest'}")
        return records[0]

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
