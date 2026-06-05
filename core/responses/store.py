"""In-memory response store for ``codexproxy`` Responses API.

v0.1 keeps every completed response in process memory and evicts entries
on a fixed TTL. ``GET /v1/responses/{id}`` and
``GET /v1/responses/{id}/input_items`` read from this store. Future
phases can swap this for SQLite or Redis without changing the route
handlers.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
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
