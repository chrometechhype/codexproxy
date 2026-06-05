"""Unit tests for the in-memory response store."""

from __future__ import annotations

import time

from core.responses.store import ResponseStore, StoredResponse


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
