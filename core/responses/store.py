"""Response store types for ``codexproxy`` Responses API.

``GET /v1/responses/{id}`` and ``GET /v1/responses/{id}/input_items``
read from this store. Future phases can swap this for SQLite or Redis
without changing the route handlers.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class StoredResponse:
    """A snapshot of a response we served, kept for retrieval."""

    id: str
    created_at: int
    completed_at: int
    model: str
    status: str
    output: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    instructions: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_choice: Any = "auto"
    temperature: float = 1.0
    top_p: float = 1.0
    parallel_tool_calls: bool = True
    previous_response_id: str | None = None
    store: bool = True
    user: str | None = None
    input_items: list[dict[str, Any]] = field(default_factory=list)
    stored_at: float = field(default_factory=time.time)

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "model": self.model,
            "status": self.status,
            "output": json.dumps(self.output),
            "usage": json.dumps(self.usage),
            "error": json.dumps(self.error),
            "instructions": self.instructions,
            "metadata": json.dumps(self.metadata),
            "tools": json.dumps(self.tools),
            "tool_choice": json.dumps(self.tool_choice),
            "temperature": self.temperature,
            "top_p": self.top_p,
            "parallel_tool_calls": int(self.parallel_tool_calls),
            "previous_response_id": self.previous_response_id,
            "store": int(self.store),
            "user": self.user,
            "input_items": json.dumps(self.input_items),
            "stored_at": self.stored_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> StoredResponse:
        d = dict(row)
        return cls(
            id=d["id"],
            created_at=d["created_at"],
            completed_at=d["completed_at"],
            model=d["model"],
            status=d["status"],
            output=json.loads(d["output"]),
            usage=json.loads(d["usage"]),
            error=json.loads(d["error"]) if d["error"] else None,
            instructions=d["instructions"],
            metadata=json.loads(d["metadata"]),
            tools=json.loads(d["tools"]),
            tool_choice=json.loads(d["tool_choice"]),
            temperature=d["temperature"],
            top_p=d["top_p"],
            parallel_tool_calls=bool(d["parallel_tool_calls"]),
            previous_response_id=d["previous_response_id"],
            store=bool(d["store"]),
            user=d["user"],
            input_items=json.loads(d["input_items"]),
            stored_at=d["stored_at"],
        )


class ResponseStore:
    """Process-local response cache with simple TTL eviction."""

    DEFAULT_TTL_SECONDS = 60.0 * 60.0  # 1h

    def __init__(self, *, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[str, StoredResponse] = {}
        self._lock = threading.Lock()

    def put(self, response: StoredResponse) -> None:
        with self._lock:
            self._entries[response.id] = response
            self._evict_expired_locked()

    def get(self, response_id: str) -> StoredResponse | None:
        with self._lock:
            self._evict_expired_locked()
            return self._entries.get(response_id)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def _evict_expired_locked(self) -> None:
        cutoff = time.time() - self._ttl
        for key in [k for k, v in self._entries.items() if v.stored_at < cutoff]:
            self._entries.pop(key, None)


class SqliteResponseStore:
    """SQLite-backed response store — durable across restarts.

    Implements the same ``put`` / ``get`` / ``clear`` interface as
    :class:`ResponseStore` so it can be swapped in transparently.
    """

    DEFAULT_TTL_SECONDS = 60.0 * 60.0  # 1h

    def __init__(
        self,
        db_path: str | Path = "responses.db",
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._db_path = Path(db_path)
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS responses (
                id TEXT PRIMARY KEY,
                created_at INTEGER NOT NULL,
                completed_at INTEGER NOT NULL,
                model TEXT NOT NULL,
                status TEXT NOT NULL,
                output TEXT NOT NULL DEFAULT '[]',
                usage TEXT NOT NULL DEFAULT '{}',
                error TEXT,
                instructions TEXT,
                metadata TEXT NOT NULL DEFAULT '{}',
                tools TEXT NOT NULL DEFAULT '[]',
                tool_choice TEXT NOT NULL DEFAULT '"auto"',
                temperature REAL NOT NULL DEFAULT 1.0,
                top_p REAL NOT NULL DEFAULT 1.0,
                parallel_tool_calls INTEGER NOT NULL DEFAULT 1,
                previous_response_id TEXT,
                store INTEGER NOT NULL DEFAULT 1,
                user TEXT,
                input_items TEXT NOT NULL DEFAULT '[]',
                stored_at REAL NOT NULL
            )"""
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_stored_at ON responses(stored_at)"
        )
        self._conn.commit()

    def put(self, response: StoredResponse) -> None:
        row = response.to_row()
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO responses (
                    id, created_at, completed_at, model, status,
                    output, usage, error, instructions, metadata,
                    tools, tool_choice, temperature, top_p,
                    parallel_tool_calls, previous_response_id, store,
                    user, input_items, stored_at
                ) VALUES (
                    :id, :created_at, :completed_at, :model, :status,
                    :output, :usage, :error, :instructions, :metadata,
                    :tools, :tool_choice, :temperature, :top_p,
                    :parallel_tool_calls, :previous_response_id, :store,
                    :user, :input_items, :stored_at
                )""",
                row,
            )
            self._evict_expired_locked()
            self._conn.commit()

    def get(self, response_id: str) -> StoredResponse | None:
        with self._lock:
            self._evict_expired_locked()
            cursor = self._conn.execute(
                "SELECT * FROM responses WHERE id = ?", (response_id,)
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return StoredResponse.from_row(row)

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM responses")
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def _evict_expired_locked(self) -> None:
        cutoff = time.time() - self._ttl
        self._conn.execute("DELETE FROM responses WHERE stored_at < ?", (cutoff,))


Store = ResponseStore | SqliteResponseStore
