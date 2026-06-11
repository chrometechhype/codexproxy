"""Unit tests for response stores (in-memory and SQLite)."""

from __future__ import annotations

import json
import time

import pytest

from core.responses.store import (
    ResponseStore,
    SqliteResponseStore,
    StoredResponse,
)


def _build(id_: str = "resp_test", **overrides: object) -> StoredResponse:
    payload: StoredResponse = StoredResponse(
        id=id_,
        created_at=1,
        completed_at=1,
        model="opencode/gpt-4o",
        status="completed",
    )
    for key, value in overrides.items():
        setattr(payload, key, value)
    return payload


# ===================================================================
# In-memory ResponseStore
# ===================================================================


def test_put_and_get_returns_stored_response() -> None:
    store = ResponseStore()
    stored = _build()
    store.put(stored)

    assert store.get("resp_test") is stored
    assert store.get("missing") is None


def test_clear_empties_store() -> None:
    store = ResponseStore()
    store.put(_build())
    store.clear()
    assert store.get("resp_test") is None


def test_eviction_removes_expired_entries() -> None:
    store = ResponseStore(ttl_seconds=0.05)
    stored = _build()
    stored.stored_at = time.time() - 1.0
    store._entries[stored.id] = stored
    # ``get`` triggers eviction on access
    assert store.get("resp_test") is None
    assert stored.id not in store._entries


def test_put_evicts_expired_entries() -> None:
    store = ResponseStore(ttl_seconds=0.05)
    stale = _build(id_="resp_stale")
    stale.stored_at = time.time() - 1.0
    store._entries[stale.id] = stale

    store.put(_build(id_="resp_fresh"))

    assert store.get("resp_stale") is None
    assert store.get("resp_fresh") is not None


# ===================================================================
# SQLite-backed SqliteResponseStore
# ===================================================================


@pytest.fixture
def sqlite_store(tmp_path: pytest.TempPathFactory) -> SqliteResponseStore:
    db = tmp_path / "test_responses.db"
    store = SqliteResponseStore(db, ttl_seconds=3600)
    yield store
    store.close()
    if db.is_file():
        db.unlink()


def test_sqlite_put_and_get(sqlite_store: SqliteResponseStore) -> None:
    stored = _build()
    sqlite_store.put(stored)
    retrieved = sqlite_store.get("resp_test")
    assert retrieved is not None
    assert retrieved.id == stored.id
    assert retrieved.created_at == stored.created_at
    assert retrieved.completed_at == stored.completed_at
    assert retrieved.model == stored.model
    assert retrieved.status == stored.status


def test_sqlite_get_missing(sqlite_store: SqliteResponseStore) -> None:
    assert sqlite_store.get("nonexistent") is None


def test_sqlite_clear(sqlite_store: SqliteResponseStore) -> None:
    sqlite_store.put(_build("resp_a"))
    sqlite_store.put(_build("resp_b"))
    sqlite_store.clear()
    assert sqlite_store.get("resp_a") is None
    assert sqlite_store.get("resp_b") is None


def test_sqlite_put_overwrites_existing(sqlite_store: SqliteResponseStore) -> None:
    sqlite_store.put(_build("resp_test", model="v1"))
    sqlite_store.put(_build("resp_test", model="v2"))
    retrieved = sqlite_store.get("resp_test")
    assert retrieved is not None
    assert retrieved.model == "v2"


def test_sqlite_persists_across_instances(tmp_path: pytest.TempPathFactory) -> None:
    db = tmp_path / "persist_test.db"
    store1 = SqliteResponseStore(db, ttl_seconds=3600)
    store1.put(_build("resp_keep", output=[{"type": "text", "text": "hello"}]))
    store1.close()

    store2 = SqliteResponseStore(db, ttl_seconds=3600)
    retrieved = store2.get("resp_keep")
    store2.close()
    db.unlink()

    assert retrieved is not None
    assert retrieved.id == "resp_keep"
    assert retrieved.output == [{"type": "text", "text": "hello"}]


def test_sqlite_round_trips_complex_fields(sqlite_store: SqliteResponseStore) -> None:
    output = [{"type": "text", "text": "hello"}, {"type": "tool_use", "name": "bash"}]
    usage = {"input_tokens": 10, "output_tokens": 20}
    meta = {"session": "abc", "tags": ["a", "b"]}
    stored = _build(
        id_="resp_complex",
        output=output,
        usage=usage,
        metadata=meta,
        instructions="be helpful",
        error={"type": "server_error", "message": "oops"},
        user="test_user",
        temperature=0.7,
        top_p=0.9,
        parallel_tool_calls=False,
        store=False,
        previous_response_id="prev_123",
        tools=[{"type": "function", "name": "foo"}],
        tool_choice={"type": "function", "name": "foo"},
        input_items=[{"type": "message", "role": "user", "content": "hi"}],
    )
    sqlite_store.put(stored)
    retrieved = sqlite_store.get("resp_complex")
    assert retrieved is not None
    assert retrieved.output == output
    assert retrieved.usage == usage
    assert retrieved.metadata == meta
    assert retrieved.instructions == "be helpful"
    assert retrieved.error == {"type": "server_error", "message": "oops"}
    assert retrieved.user == "test_user"
    assert retrieved.temperature == 0.7
    assert retrieved.top_p == 0.9
    assert retrieved.parallel_tool_calls is False
    assert retrieved.store is False
    assert retrieved.previous_response_id == "prev_123"
    assert retrieved.tools == [{"type": "function", "name": "foo"}]
    assert retrieved.tool_choice == {"type": "function", "name": "foo"}
    assert retrieved.input_items == [
        {"type": "message", "role": "user", "content": "hi"}
    ]


def test_sqlite_round_trips_none_error(sqlite_store: SqliteResponseStore) -> None:
    stored = _build(id_="resp_no_err", error=None, instructions=None, user=None)
    sqlite_store.put(stored)
    retrieved = sqlite_store.get("resp_no_err")
    assert retrieved is not None
    assert retrieved.error is None
    assert retrieved.instructions is None
    assert retrieved.user is None


def test_sqlite_eviction(sqlite_store: SqliteResponseStore) -> None:
    sqlite_store._ttl = 0.001
    stored = _build()
    stored.stored_at = time.time() - 60.0
    sqlite_store.put(stored)
    time.sleep(0.01)
    assert sqlite_store.get("resp_test") is None


def test_sqlite_eviction_on_put(sqlite_store: SqliteResponseStore) -> None:
    sqlite_store._ttl = 0.1
    stale = _build(id_="resp_stale")
    stale.stored_at = time.time() - 60.0
    sqlite_store.put(stale)

    fresh = _build(id_="resp_fresh", stored_at=time.time())
    sqlite_store.put(fresh)

    assert sqlite_store.get("resp_stale") is None
    retrieved = sqlite_store.get("resp_fresh")
    assert retrieved is not None
    assert retrieved.id == "resp_fresh"


def test_sqlite_to_row_from_row_round_trip() -> None:
    output = [{"type": "text", "text": "hi"}]
    usage = {"input_tokens": 5}
    stored = _build(
        id_="resp_row",
        output=output,
        usage=usage,
        metadata={"k": "v"},
        error=None,
        instructions=None,
        temperature=0.5,
        top_p=0.8,
        parallel_tool_calls=True,
        store=True,
        user=None,
        input_items=[],
        tools=[],
        previous_response_id=None,
        tool_choice="auto",
    )
    row = stored.to_row()
    assert json.loads(row["output"]) == output
    assert json.loads(row["usage"]) == usage
    assert row["temperature"] == 0.5
    assert row["parallel_tool_calls"] == 1
    assert row["store"] == 1
    assert row["error"] == json.dumps(None)  # json.dumps(None) -> "null"
