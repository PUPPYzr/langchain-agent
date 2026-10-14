"""SQLite-backed chat transcript for the Web UI.

This stores the *view* of a conversation (what the user should see). The Agent's
own conversational memory lives in the LangGraph checkpointer, keyed by the same
thread id; the two are deliberately separate concerns.

Persistence: one SQLite file (``SERVICE_SESSION_DB_PATH``, defaults to the same
file as the Run ledger). Sessions and messages survive a restart; nothing is
kept in memory only.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock

VALID_ROLES = frozenset({"user", "assistant"})
TITLE_MAX_LENGTH = 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class ChatSession:
    """One conversation owned by a tenant."""

    session_id: str
    tenant_id: str
    title: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One rendered turn in a conversation."""

    session_id: str
    sequence: int
    role: str
    content: str
    created_at: str


class SessionNotFoundError(KeyError):
    """Raised when a session does not exist or belongs to another tenant."""


def derive_title(message: str) -> str:
    """Build a short session title from the first user message."""
    collapsed = " ".join(message.split())
    if len(collapsed) <= TITLE_MAX_LENGTH:
        return collapsed or "新对话"
    return collapsed[: TITLE_MAX_LENGTH - 1] + "…"


class ChatSessionStore:
    """Persist sessions and their messages for one service instance."""

    def __init__(self, path: str = "agent_runs.sqlite3") -> None:
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_sessions (
                session_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                session_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (session_id, sequence)
            )
            """
        )
        self._connection.commit()
        self._lock = RLock()

    # --- sessions ----------------------------------------------------------

    def create(self, tenant_id: str, title: str = "新对话") -> ChatSession:
        """Create an empty session for a tenant."""
        session_id = _new_session_id()
        timestamp = _now()
        with self._lock:
            self._connection.execute(
                "INSERT INTO chat_sessions (session_id, tenant_id, title, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (session_id, tenant_id, title, timestamp, timestamp),
            )
            self._connection.commit()
        return ChatSession(
            session_id=session_id,
            tenant_id=tenant_id,
            title=title,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def get(self, tenant_id: str, session_id: str) -> ChatSession:
        """Return one session, hiding other tenants' sessions as missing."""
        with self._lock:
            row = self._connection.execute(
                "SELECT session_id, tenant_id, title, created_at, updated_at"
                " FROM chat_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None or row[1] != tenant_id:
            raise SessionNotFoundError(session_id)
        return ChatSession(
            session_id=row[0],
            tenant_id=row[1],
            title=row[2],
            created_at=row[3],
            updated_at=row[4],
        )

    def list(self, tenant_id: str, *, limit: int | None = None) -> list[ChatSession]:
        """List a tenant's sessions, most recently updated first."""
        query = (
            "SELECT session_id, tenant_id, title, created_at, updated_at"
            " FROM chat_sessions WHERE tenant_id = ? ORDER BY updated_at DESC"
        )
        params: list[object] = [tenant_id]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [
            ChatSession(
                session_id=row[0],
                tenant_id=row[1],
                title=row[2],
                created_at=row[3],
                updated_at=row[4],
            )
            for row in rows
        ]

    def delete(self, tenant_id: str, session_id: str) -> None:
        """Delete a session and its transcript."""
        self.get(tenant_id, session_id)
        with self._lock:
            self._connection.execute(
                "DELETE FROM chat_messages WHERE session_id = ?", (session_id,)
            )
            self._connection.execute(
                "DELETE FROM chat_sessions WHERE session_id = ?", (session_id,)
            )
            self._connection.commit()

    # --- messages ----------------------------------------------------------

    def append_message(
        self,
        tenant_id: str,
        session_id: str,
        role: str,
        content: str,
    ) -> ChatMessage:
        """Append one turn and touch the session."""
        if role not in VALID_ROLES:
            raise ValueError(f"unsupported role: {role}")
        session = self.get(tenant_id, session_id)
        timestamp = _now()
        with self._lock:
            next_sequence = (
                self._connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM chat_messages"
                    " WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
            )
            self._connection.execute(
                "INSERT INTO chat_messages (session_id, sequence, role, content, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (session_id, next_sequence, role, content, timestamp),
            )
            title = session.title
            if role == "user" and title == "新对话":
                title = derive_title(content)
            self._connection.execute(
                "UPDATE chat_sessions SET title = ?, updated_at = ? WHERE session_id = ?",
                (title, timestamp, session_id),
            )
            self._connection.commit()
        return ChatMessage(
            session_id=session_id,
            sequence=next_sequence,
            role=role,
            content=content,
            created_at=timestamp,
        )

    def messages(self, tenant_id: str, session_id: str) -> list[ChatMessage]:
        """Return a session transcript in order."""
        self.get(tenant_id, session_id)
        with self._lock:
            rows = self._connection.execute(
                "SELECT session_id, sequence, role, content, created_at"
                " FROM chat_messages WHERE session_id = ? ORDER BY sequence",
                (session_id,),
            ).fetchall()
        return [
            ChatMessage(
                session_id=row[0],
                sequence=row[1],
                role=row[2],
                content=row[3],
                created_at=row[4],
            )
            for row in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "ChatSessionStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _new_session_id() -> str:
    from uuid import uuid4

    return uuid4().hex[:16]
