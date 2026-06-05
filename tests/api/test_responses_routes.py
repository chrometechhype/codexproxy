"""Integration tests for ``/v1/responses`` and related routes."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from core.responses.store import ResponseStore, StoredResponse

# A local module-level app avoids touching the global ``app`` fixture used by
# other api tests; this is the same pattern as ``tests/api/test_api.py``.
app = create_app()


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _StubProvider:
    """Streams a canned Anthropic-format SSE sequence."""

    def __init__(self) -> None:
        self.last_messages_request: Any = None
        self.last_thinking_enabled: bool | None = None

    async def stream_response(
        self, messages_request, *, input_tokens, request_id, thinking_enabled
    ):
        self.last_messages_request = messages_request
        self.last_thinking_enabled = thinking_enabled
        yield (
            "event: message_start\n"
            'data: {"message":{"id":"msg_1","usage":{"input_tokens":1,'
            '"output_tokens":0}}}\n\n'
        )
        yield (
            "event: content_block_start\n"
            'data: {"index":0,"content_block":{"type":"text","text":""}}\n\n'
        )
        yield (
            "event: content_block_delta\n"
            'data: {"index":0,"delta":{"type":"text_delta","text":"hello"}}\n\n'
        )
        yield 'event: content_block_stop\ndata: {"index":0}\n\n'
        yield 'event: message_delta\ndata: {"usage":{"output_tokens":5}}\n\n'
        yield "event: message_stop\ndata: {}\n\n"


@pytest.fixture
def stub_provider() -> _StubProvider:
    return _StubProvider()


@pytest.fixture
def client(stub_provider: _StubProvider, monkeypatch: pytest.MonkeyPatch):
    """A ``TestClient`` that bypasses the lifespan and shares a seeded store."""
    # Force the auth token so the dependency-injected guard accepts requests
    # without polluting the global env for other test files.
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "freecc")
    monkeypatch.setenv("CODEX_PROXY_AUTH_TOKEN", "freecc")
    # Pre-seed ``app.state.responses_store`` with a known response so retrieval
    # tests work without a live provider call.
    store = ResponseStore()
    store.put(
        StoredResponse(
            id="resp_seeded",
            created_at=10,
            completed_at=11,
            model="gpt-4o",
            status="completed",
            output=[
                {
                    "id": "msg_seed",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": "seeded", "annotations": []}
                    ],
                }
            ],
            usage={
                "input_tokens": 1,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 2,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 3,
            },
            instructions=None,
            metadata={},
            tools=[],
            tool_choice="auto",
            temperature=1.0,
            top_p=1.0,
            parallel_tool_calls=True,
            previous_response_id=None,
            store=True,
            user=None,
            input_items=[{"type": "message", "role": "user", "content": "hi"}],
        )
    )
    app.state.responses_store = store
    # ``TestClient(app, raise_server_exceptions=True)`` does not enter the
    # FastAPI lifespan context (no startup/shutdown), so the seed survives.
    with (
        patch("api.dependencies.resolve_provider", return_value=stub_provider),
        TestClient(app) as test_client,
    ):
        # The lifespan startup may re-initialise ``app.state``; re-seed the
        # store after entering the context to keep retrieval tests stable.
        state = getattr(test_client.app, "state", None)
        if state is not None:
            state.responses_store = store
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer freecc",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# /v1/models (Responses shape)
# ---------------------------------------------------------------------------


def test_models_list_contains_configured_model(client: TestClient) -> None:
    """The default ``MODEL`` is exposed (either Anthropic or Responses shape)."""
    response = client.get("/v1/models", headers={"Authorization": "Bearer freecc"})
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data["data"], list)
    ids = {entry["id"] for entry in data["data"]}
    # The default MODEL is exposed; either as the bare id, as
    # ``provider/model``, or as ``anthropic/provider/model`` depending on
    # the path that resolves first.
    assert (
        "nvidia_nim/test-model" in ids
        or "test-model" in ids
        or "anthropic/nvidia_nim/test-model" in ids
    )


# ---------------------------------------------------------------------------
# POST /v1/responses (non-streaming)
# ---------------------------------------------------------------------------


def test_post_responses_non_streaming_returns_resource(
    client: TestClient, auth_headers: dict[str, str], stub_provider: _StubProvider
) -> None:
    body = {"model": "gpt-4o", "input": "hi", "stream": False}
    response = client.post("/v1/responses", headers=auth_headers, json=body)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["output"][0]["content"][0]["text"] == "hello"
    rid = data["id"]
    assert rid.startswith("resp_")


def test_post_responses_persists_to_shared_store(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    body = {"model": "gpt-4o", "input": "hi", "stream": False}
    response = client.post("/v1/responses", headers=auth_headers, json=body)
    assert response.status_code == 200
    rid = response.json()["id"]
    # Retrieve the stored response via the public GET route — the shared
    # store keeps it accessible across requests.
    get = client.get(f"/v1/responses/{rid}", headers=auth_headers)
    assert get.status_code == 200
    assert get.json()["id"] == rid


def test_post_responses_forwards_to_provider(
    client: TestClient, auth_headers: dict[str, str], stub_provider: _StubProvider
) -> None:
    body = {
        "model": "gpt-4o",
        "input": [
            {"type": "message", "role": "user", "content": "hi"},
        ],
        "stream": False,
        "instructions": "be brief",
    }
    response = client.post("/v1/responses", headers=auth_headers, json=body)
    assert response.status_code == 200
    assert stub_provider.last_messages_request is not None
    req = stub_provider.last_messages_request
    assert req.model == "test-model"
    assert req.messages[0].role == "user"


# ---------------------------------------------------------------------------
# POST /v1/responses (streaming)
# ---------------------------------------------------------------------------


def test_post_responses_streaming_emits_completed_event(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    body = {"model": "gpt-4o", "input": "hi", "stream": True}
    response = client.post("/v1/responses", headers=auth_headers, json=body)
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    raw = b"".join(response.iter_bytes()).decode()
    assert "event: response.created" in raw
    assert "event: response.in_progress" in raw
    assert "event: response.output_text.delta" in raw
    assert "event: response.completed" in raw


# ---------------------------------------------------------------------------
# GET /v1/responses/{id}
# ---------------------------------------------------------------------------


def test_get_response_returns_stored_payload(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.get("/v1/responses/resp_seeded", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "resp_seeded"
    assert data["output"][0]["content"][0]["text"] == "seeded"


def test_get_response_404_for_missing(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.get("/v1/responses/resp_missing", headers=auth_headers)
    assert response.status_code == 404
    assert response.json() == {"detail": {"error": "Response not found."}}


# ---------------------------------------------------------------------------
# GET /v1/responses/{id}/input_items
# ---------------------------------------------------------------------------


def test_get_input_items_returns_seeded_input(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.get("/v1/responses/resp_seeded/input_items", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "list"
    assert len(data["data"]) == 1
    assert data["data"][0]["type"] == "message"


def test_get_input_items_404_for_missing(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.get(
        "/v1/responses/resp_missing/input_items", headers=auth_headers
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /v1/responses (list stub)
# ---------------------------------------------------------------------------


def test_list_responses_stub(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.get("/v1/responses", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "list"
    assert data["data"] == []
    assert data["has_more"] is False


# ---------------------------------------------------------------------------
# POST /v1/conversations (stub)
# ---------------------------------------------------------------------------


def test_create_conversation_stub(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post("/v1/conversations", headers=auth_headers, json={})
    assert response.status_code == 200
    data = response.json()
    assert data["id"].startswith("conv_")
    assert data["object"] == "conversation"
    assert isinstance(data["created_at"], int)
