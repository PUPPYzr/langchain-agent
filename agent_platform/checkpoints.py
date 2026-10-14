"""Platform checkpoint provider contracts and durable LangGraph storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import pickle
import sqlite3
from typing import Any, Mapping, Protocol

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver


def _checkpoint_serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=None)


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
    """Query and explicitly clean up checkpoints without exposing graph state."""

    def get(self, thread_id: str, checkpoint_id: str | None = None) -> CheckpointRecord:
        """Return the requested or latest checkpoint for a thread."""

    def list(
        self,
        thread_id: str | None = None,
        *,
        limit: int | None = None,
    ) -> tuple[CheckpointRecord, ...]:
        """List checkpoint summaries, newest first."""

    def delete_thread(self, thread_id: str) -> None:
        """Delete all checkpoint data for one thread."""


class NullCheckpointProvider:
    """Disable checkpoint persistence explicitly."""

    def create_checkpointer(self) -> None:
        return None


class InMemoryLangGraphCheckpointProvider:
    """Create LangGraph's in-memory checkpointer for local runs and tests."""

    def create_checkpointer(self) -> Any:
        return InMemorySaver()


class SQLiteLangGraphCheckpointSaver(SqliteSaver):
    """Managed LangGraph SQLite saver with one-time legacy snapshot migration."""

    _MIGRATION_KEY = "legacy-pickle-snapshots-to-langgraph-sqlite-v1"

    def __init__(self, path: str) -> None:
        connection = sqlite3.connect(path, check_same_thread=False)
        super().__init__(connection, serde=_checkpoint_serializer())
        self.setup()

    def migrate_legacy_snapshots(self) -> int:
        """Copy snapshots from the former platform table into LangGraph tables."""
        legacy_table = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'agent_checkpoints'"
        ).fetchone()
        if legacy_table is None:
            return 0

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_checkpoint_migrations (
                migration_key TEXT PRIMARY KEY,
                completed_at TEXT NOT NULL,
                migrated_count INTEGER NOT NULL
            )
            """
        )
        migrated = self.conn.execute(
            "SELECT migrated_count FROM agent_checkpoint_migrations WHERE migration_key = ?",
            (self._MIGRATION_KEY,),
        ).fetchone()
        if migrated is not None:
            return int(migrated[0])

        rows = self.conn.execute(
            """
            SELECT thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
                   checkpoint_blob, metadata_blob
            FROM agent_checkpoints
            ORDER BY created_at, checkpoint_id
            """
        ).fetchall()
        for thread_id, namespace, checkpoint_id, parent_id, checkpoint_blob, metadata_blob in rows:
            checkpoint = pickle.loads(checkpoint_blob)
            metadata = pickle.loads(metadata_blob)
            if not isinstance(checkpoint, dict) or checkpoint.get("id") != checkpoint_id:
                raise ValueError(
                    f"Invalid legacy checkpoint snapshot: {thread_id}/{checkpoint_id}"
                )
            if not isinstance(metadata, dict):
                raise ValueError(
                    f"Invalid legacy checkpoint metadata: {thread_id}/{checkpoint_id}"
                )

            configurable = {
                "thread_id": thread_id,
                "checkpoint_ns": namespace,
            }
            if parent_id is not None:
                configurable["checkpoint_id"] = parent_id
            self.put(
                {"configurable": configurable},
                checkpoint,
                metadata,
                checkpoint.get("channel_versions", {}),
            )

        self.conn.execute(
            """
            INSERT INTO agent_checkpoint_migrations (
                migration_key, completed_at, migrated_count
            ) VALUES (?, ?, ?)
            """,
            (
                self._MIGRATION_KEY,
                datetime.now(timezone.utc).isoformat(),
                len(rows),
            ),
        )
        self.conn.commit()
        return len(rows)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "SQLiteLangGraphCheckpointSaver":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class SQLiteCheckpointProvider:
    """Create LangGraph's SQLite saver and query repository."""

    def __init__(self, path: str = "agent_runs.sqlite3") -> None:
        self.path = path

    def create_checkpointer(self) -> SQLiteLangGraphCheckpointSaver:
        saver = SQLiteLangGraphCheckpointSaver(self.path)
        try:
            saver.migrate_legacy_snapshots()
        except Exception:
            saver.close()
            raise
        return saver

    def repository(self) -> "SQLiteCheckpointRepository":
        return SQLiteCheckpointRepository(self.path)


class SQLiteCheckpointRepository:
    """Read checkpoint summaries from LangGraph's SQLite tables."""

    def __init__(self, path: str = "agent_runs.sqlite3") -> None:
        self._saver = SQLiteLangGraphCheckpointSaver(path)
        try:
            self._saver.migrate_legacy_snapshots()
        except Exception:
            self._saver.close()
            raise
        self._connection = self._saver.conn

    def list(
        self,
        thread_id: str | None = None,
        *,
        limit: int | None = None,
    ) -> tuple[CheckpointRecord, ...]:
        config = (
            {"configurable": {"thread_id": thread_id}}
            if thread_id is not None
            else None
        )
        records = []
        for item in self._saver.list(config):
            configurable = item.config["configurable"]
            parent_config = item.parent_config or {}
            parent_configurable = parent_config.get("configurable", {})
            checkpoint = item.checkpoint
            records.append(
                CheckpointRecord(
                    thread_id=configurable["thread_id"],
                    checkpoint_id=configurable["checkpoint_id"],
                    checkpoint_ns=configurable.get("checkpoint_ns", ""),
                    created_at=str(
                        checkpoint.get("ts")
                        or item.metadata.get("created_at")
                        or ""
                    ),
                    parent_checkpoint_id=parent_configurable.get("checkpoint_id"),
                    metadata=item.metadata,
                )
            )
        records.sort(key=lambda record: record.created_at, reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def get(self, thread_id: str, checkpoint_id: str | None = None) -> CheckpointRecord:
        records = self.list(thread_id)
        if checkpoint_id is not None:
            records = tuple(item for item in records if item.checkpoint_id == checkpoint_id)
        if not records:
            raise KeyError(f"Checkpoint not found: {thread_id}/{checkpoint_id or 'latest'}")
        return records[0]

    def delete_thread(self, thread_id: str) -> None:
        self._saver.delete_thread(thread_id)
        legacy_table = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'agent_checkpoints'"
        ).fetchone()
        if legacy_table is not None:
            self._connection.execute(
                "DELETE FROM agent_checkpoints WHERE thread_id = ?",
                (thread_id,),
            )
        self._connection.commit()

    def close(self) -> None:
        self._saver.close()

    def __enter__(self) -> "SQLiteCheckpointRepository":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
